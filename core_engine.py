"""Motor de automatización: campañas diarias con cron (APScheduler).

Cómo funciona:
- Cada campaña (cuenta + género + hora) se guarda en la base de datos: ESA es la fuente de verdad.
- Al arrancar el servidor se vuelve a registrar un trabajo `cron` por cada campaña activa,
  así nada se pierde en reinicios ni en deploys.
- Si el servidor estaba dormido/apagado a la hora programada, `ejecutar_pendientes()` recupera
  la ejecución de hoy (al arrancar y cada vez que algo llame a /api/cron/tick).
- Cada ejecución crea `volumen` listas con una pausa entre ellas, rota los artistas para no
  repetirlos, registra el historial y avisa por Telegram.
"""
import logging
import random
import threading
import time
from datetime import timedelta

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func

from auth_db import (
    ArtistaUsadoDB, CampanaDB, EjecucionDB, PlaylistCreadaDB, SemillaDB, SessionLocal,
    cargar_imagen, utcnow,
)
from logic import (
    ESTADO_EN_CURSO, ESTADOS_FINALES, MAX_INTENTOS_DIA, debe_ejecutar, elegir_artistas,
    lineas_artistas,
)
from notificaciones import notificar
from spotify_utils import (
    CuotaAgotada, SpotifyClient, crear_playlist_para_artista, eliminar_playlist,
)
from tokens import obtener_access_token

logger = logging.getLogger(__name__)

TZ = pytz.timezone("America/Caracas")
scheduler = BackgroundScheduler(
    timezone=TZ,
    executors={"default": ThreadPoolExecutor(25)},
    # coalesce: si se acumulan ejecuciones perdidas, corre una sola.
    # misfire_grace_time: tolera 1 hora de retraso (CPU saturada, etc.).
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
)

_inicio_lock = threading.Lock()  # evita que el cron y el "tick" arranquen la misma campaña a la vez


def ahora_local():
    return pytz.utc.localize(utcnow()).astimezone(TZ)


def a_local(dt_utc):
    return pytz.utc.localize(dt_utc).astimezone(TZ) if dt_utc else None


def id_trabajo(campana_id: int) -> str:
    return f"camp_{campana_id}"


# ------------------------------------------------------------------ programación
def registrar_campana(c) -> None:
    """Crea (o reemplaza) el trabajo cron diario de una campaña activa."""
    scheduler.add_job(
        ejecutar_campana,
        CronTrigger(hour=c.hora, minute=c.minuto, timezone=TZ),
        args=[c.id],
        id=id_trabajo(c.id),
        replace_existing=True,
    )


def quitar_campana(campana_id: int) -> None:
    if scheduler.get_job(id_trabajo(campana_id)):
        scheduler.remove_job(id_trabajo(campana_id))


def cargar_campanas() -> int:
    """Al arrancar: vuelve a registrar el cron de todas las campañas activas."""
    db = SessionLocal()
    try:
        activas = db.query(CampanaDB).filter(CampanaDB.activa.is_(True)).all()
        for c in activas:
            registrar_campana(c)
        return len(activas)
    finally:
        db.close()


def marcar_interrumpidas() -> int:
    """Ejecuciones que quedaron 'En curso' porque el servidor se reinició a mitad de camino."""
    db = SessionLocal()
    try:
        filas = db.query(EjecucionDB).filter(EjecucionDB.estado == ESTADO_EN_CURSO).all()
        for e in filas:
            e.estado = "Interrumpida"
            e.fin = utcnow()
            e.detalle = (e.detalle or "") + "\nEl servidor se reinició durante la ejecución."
        db.commit()
        return len(filas)
    finally:
        db.close()


def ejecutar_pendientes() -> list:
    """Lanza las campañas de hoy que ya debieron correr y no corrieron (servidor dormido o reiniciado).

    Es idempotente: se puede llamar cuantas veces se quiera (arranque del servidor, endpoint
    /api/cron/tick llamado cada 5-15 min por un pinger externo, etc.).
    """
    lanzadas = []
    ahora = ahora_local()
    db = SessionLocal()
    try:
        for c in db.query(CampanaDB).filter(CampanaDB.activa.is_(True)).all():
            estados = [
                e.estado for e in db.query(EjecucionDB).filter(
                    EjecucionDB.campana_id == c.id, EjecucionDB.fecha == ahora.date()
                )
            ]
            if debe_ejecutar(c.hora, c.minuto, ahora, a_local(c.creada_en), estados):
                scheduler.add_job(
                    ejecutar_campana, args=[c.id], id=f"ahora_{c.id}_{int(time.time())}",
                    replace_existing=True,
                )
                lanzadas.append(c.id)
    finally:
        db.close()
    return lanzadas


# ------------------------------------------------------------------ ejecución
def ejecutar_campana(campana_id: int, manual: bool = False) -> None:
    """Punto de entrada del cron (y del botón 'Ejecutar ahora' con manual=True)."""
    ahora = ahora_local()
    with _inicio_lock:
        db = SessionLocal()
        try:
            c = db.get(CampanaDB, campana_id)
            if not c:
                return
            if not manual and not c.activa:
                return
            previas = [
                e.estado for e in db.query(EjecucionDB).filter(
                    EjecucionDB.campana_id == c.id, EjecucionDB.fecha == ahora.date()
                )
            ]
            if ESTADO_EN_CURSO in previas:
                logger.info("Campaña %s ya está corriendo, se omite", campana_id)
                return
            if not manual and (any(p in ESTADOS_FINALES for p in previas) or len(previas) >= MAX_INTENTOS_DIA):
                logger.info("Campaña %s ya se ejecutó hoy, se omite", campana_id)
                return
            ej = EjecucionDB(
                campana_id=c.id, cuenta=c.cuenta, genero=c.genero, volumen=c.volumen,
                estado=ESTADO_EN_CURSO, fecha=ahora.date(), inicio=utcnow(), manual=manual,
            )
            db.add(ej)
            db.commit()
            ejecucion_id = ej.id
            datos = {
                "cuenta": c.cuenta, "genero": c.genero, "volumen": c.volumen,
                "pausa": max(1, c.pausa_seg or 10), "retener_dias": c.retener_dias or 0,
                "idioma": c.idioma or "es", "extras": lineas_artistas(c.extras),
            }
        finally:
            db.close()
    _procesar(ejecucion_id, datos)


def _procesar(ejecucion_id: int, d: dict) -> None:
    db = SessionLocal()
    cuenta = d["cuenta"]
    estado, creadas, fallidas, errores = None, 0, 0, []
    solicitadas = 0
    try:
        ej = db.get(EjecucionDB, ejecucion_id)
        proveedor = lambda: obtener_access_token(cuenta)  # noqa: E731

        if not proveedor():
            estado = "Falla: Sin token"
            errores.append("La cuenta no está conectada o Spotify revocó el permiso. Reconéctala en el panel.")
            return

        semilla = db.get(SemillaDB, d["genero"])
        candidatos = lineas_artistas(semilla.artistas if semilla else "")
        if not candidatos:
            estado = "Falla: Semilla vacía"
            errores.append(f"El género '{d['genero']}' no tiene artistas.")
            return

        usados = {
            r[0] for r in db.query(ArtistaUsadoDB.artista_key).filter(
                ArtistaUsadoDB.cuenta == cuenta, ArtistaUsadoDB.genero == d["genero"]
            )
        }
        seleccion, reinicio = elegir_artistas(candidatos, usados, d["volumen"])
        if reinicio:  # se agotó la lista: empieza un ciclo nuevo
            db.query(ArtistaUsadoDB).filter(
                ArtistaUsadoDB.cuenta == cuenta, ArtistaUsadoDB.genero == d["genero"]
            ).delete()
            db.commit()
        solicitadas = len(seleccion)
        ej.volumen = solicitadas
        db.commit()

        cliente = SpotifyClient(proveedor, cuenta)
        try:
            borradas = _limpiar_antiguas(cliente, db, cuenta, d["retener_dias"])
            if borradas:
                errores.append(f"(Limpieza: {borradas} playlists antiguas eliminadas)")
        except CuotaAgotada as e:
            estado = "Pausada: cuota"
            errores.append(str(e))
            return

        for i, artista in enumerate(seleccion):
            try:
                res = crear_playlist_para_artista(
                    cliente, artista, language=d["idioma"], id_extra=d["extras"],
                    imagen_loader=cargar_imagen,
                )
            except CuotaAgotada as e:
                estado = "Pausada: cuota"
                errores.append(str(e))
                break

            if res["status"] == "auth_required":
                estado = "Falla: Sin token"
                errores.append("El token dejó de ser válido durante la ejecución.")
                break

            if res["status"] == "success":
                creadas += 1
                db.add(PlaylistCreadaDB(cuenta=cuenta, playlist_id=res["playlist_id"], artista=artista))
            else:
                fallidas += 1
                errores.append(f"{artista}: {res.get('message', 'error')}")
            if res["status"] == "success" or res.get("definitivo"):
                ya_marcado = db.query(ArtistaUsadoDB.id).filter(
                    ArtistaUsadoDB.cuenta == cuenta, ArtistaUsadoDB.genero == d["genero"],
                    ArtistaUsadoDB.artista_key == artista.lower(),
                ).first()
                if not ya_marcado:  # dos campañas de la misma cuenta+género comparten la rotación
                    db.add(ArtistaUsadoDB(cuenta=cuenta, genero=d["genero"], artista_key=artista.lower()))
            ej.creadas, ej.fallidas = creadas, fallidas
            db.commit()  # el panel ve el progreso en vivo

            if i < len(seleccion) - 1:
                time.sleep(max(1.0, d["pausa"] * random.uniform(0.7, 1.3)))

        if estado is None:
            if creadas == 0:
                estado = "Falla: Error Spotify"
            elif fallidas == 0:
                estado = "Completada"
            else:
                estado = "Parcial"
    except Exception as e:
        logger.exception("Error crítico en la campaña de %s", cuenta)
        db.rollback()
        estado = "Falla: Crash interno"
        errores.append(f"{type(e).__name__}: {e}")
    finally:
        try:
            ej = db.get(EjecucionDB, ejecucion_id)
            ej.estado = estado or "Falla: Crash interno"
            ej.creadas, ej.fallidas = creadas, fallidas
            ej.fin = utcnow()
            ej.detalle = "\n".join(errores[:25])
            db.commit()
            resumen = (
                f"{'✅' if ej.estado in ('Completada', 'Parcial') else '⚠️'} <b>{ej.estado}</b>\n"
                f"Cuenta: <code>{cuenta}</code> | Género: {d['genero']}\n"
                f"Listas: {creadas}/{solicitadas or d['volumen']}"
            )
        except Exception:
            logger.exception("No se pudo guardar el resultado de la ejecución %s", ejecucion_id)
            resumen = f"⚠️ Error guardando el resultado de {cuenta}"
        finally:
            db.close()
        notificar(resumen)


def _limpiar_antiguas(cliente, db, cuenta: str, dias: int) -> int:
    """Si la campaña tiene retención (dias > 0), elimina las playlists creadas hace más de N días."""
    if dias <= 0:
        return 0
    limite = utcnow() - timedelta(days=dias)
    viejas = (
        db.query(PlaylistCreadaDB)
        .filter(PlaylistCreadaDB.cuenta == cuenta, PlaylistCreadaDB.creada_en < limite)
        .order_by(PlaylistCreadaDB.creada_en)
        .limit(200)
        .all()
    )
    borradas = 0
    for p in viejas:
        try:
            eliminar_playlist(cliente, p.playlist_id)
        except CuotaAgotada:
            raise
        except Exception as e:
            logger.warning("No se pudo eliminar la playlist %s: %s", p.playlist_id, e)
            continue
        db.delete(p)
        borradas += 1
        db.commit()
        time.sleep(1)
    return borradas


# ------------------------------------------------------------------ datos para el panel
def total_listas() -> int:
    db = SessionLocal()
    try:
        return int(db.query(func.coalesce(func.sum(EjecucionDB.creadas), 0)).scalar() or 0)
    finally:
        db.close()


def _formato_fecha(dt) -> str:
    return dt.strftime("%d/%m/%Y %H:%M:%S") if dt else "—"


def estado_reloj() -> dict:
    db = SessionLocal()
    try:
        campanas = db.query(CampanaDB).order_by(CampanaDB.id).all()
        tareas, proximos = [], []
        for c in campanas:
            trabajo = scheduler.get_job(id_trabajo(c.id)) if c.activa else None
            proxima = trabajo.next_run_time if trabajo else None
            if proxima:
                proximos.append((proxima, c))
            tareas.append({
                "id": id_trabajo(c.id),
                "cuenta": c.cuenta,
                "genero": c.genero,
                "volumen": c.volumen,
                "estado": "Pendiente" if c.activa else "Pausada",
                "fecha_ejecucion": _formato_fecha(proxima),
                "timestamp": proxima.timestamp() if proxima else 0,
                "hora": f"{c.hora:02d}:{c.minuto:02d}",
                "retener_dias": c.retener_dias or 0,
                "activa": bool(c.activa),
                "campana_id": c.id,
            })

        recientes = db.query(EjecucionDB).order_by(EjecucionDB.id.desc()).limit(40).all()
        for e in recientes:
            en_curso = e.estado == ESTADO_EN_CURSO
            tareas.append({
                "id": f"ej_{e.id}",
                "cuenta": e.cuenta,
                "genero": e.genero,
                "volumen": e.volumen,
                "estado": f"En curso ({e.creadas}/{e.volumen})" if en_curso else e.estado,
                "fecha_ejecucion": _formato_fecha(a_local(e.inicio)),
                "timestamp": 0,
                "detalle": e.detalle or "",
                "creadas": e.creadas,
                "en_curso": en_curso,
            })

        base = {"tareas_detalladas": tareas, "total_listas_creadas": total_listas()}
        if not proximos:
            return {**base, "estado": "inactivo", "cuenta_regresiva": "00:00:00",
                    "proxima_cuenta": "Ninguna", "tareas_en_cola": 0}

        proxima, c = min(proximos, key=lambda x: x[0])
        segundos = int((proxima - ahora_local()).total_seconds())
        if segundos <= 0:
            reloj = "Ejecutando..."
        else:
            dias, resto = divmod(segundos, 86400)
            horas, resto = divmod(resto, 3600)
            minutos, segs = divmod(resto, 60)
            reloj = f"{dias}d {horas:02d}:{minutos:02d}:{segs:02d}" if dias else f"{horas:02d}:{minutos:02d}:{segs:02d}"
        return {**base, "estado": "activo", "cuenta_regresiva": reloj,
                "proximo_timestamp": proxima.timestamp(),
                "proxima_cuenta": c.cuenta, "tareas_en_cola": len(proximos)}
    finally:
        db.close()


__all__ = [
    "scheduler", "registrar_campana", "quitar_campana", "cargar_campanas", "marcar_interrumpidas",
    "ejecutar_pendientes", "ejecutar_campana", "estado_reloj", "total_listas",
    "ahora_local", "TZ",
]
