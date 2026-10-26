import hmac
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

import core_engine
from auth_db import (
    CampanaDB, EjecucionDB, SemillaDB, UsuarioDB, get_db, get_password_hash, guardar_imagen,
    cargar_imagen, init_db, utcnow, verificar_password,
)
from logic import lineas_artistas, normalizar_cuenta, parse_hora
from spotify_utils import (
    crear_listas_por_nombres, crear_playlist_para_artista, obtener_auth_manager,
    obtener_credenciales, preparar_jpeg, SpotifyClient, CuotaAgotada,
)
import spotipy
import tokens

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).parent
(BASE_DIR / "static").mkdir(exist_ok=True)

ADMINS = {normalizar_cuenta(u) for u in os.getenv("ADMIN_USERS", "miguel").split(",") if u.strip()}
REGISTRO_ABIERTO = os.getenv("REGISTRO_ABIERTO", "0") == "1"
CRON_SECRET = os.getenv("CRON_SECRET", "")


# ------------------------------------------------------------------ arranque
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    interrumpidas = core_engine.marcar_interrumpidas()
    core_engine.scheduler.start()
    n = core_engine.cargar_campanas()
    recuperadas = core_engine.ejecutar_pendientes()
    logger.info("Core listo: %s campañas activas, %s interrumpidas, %s recuperadas", n, interrumpidas, recuperadas)
    yield
    core_engine.scheduler.shutdown(wait=False)


app = FastAPI(title="Spotify API 2.0", lifespan=lifespan)

_secret = os.getenv("SESSION_SECRET", "")
if len(_secret) < 32:
    # Nunca se usa una clave fija escrita en el código. Sin SESSION_SECRET las sesiones se pierden al reiniciar.
    logger.warning("SESSION_SECRET no definido (o menor a 32 caracteres): usando una clave temporal.")
    _secret = secrets.token_urlsafe(48)

app.add_middleware(
    SessionMiddleware,
    secret_key=_secret,
    session_cookie="sid",
    max_age=60 * 60 * 24 * 7,
    https_only=os.getenv("HTTPS_ONLY", "1") == "1",
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ------------------------------------------------------------------ sesión y permisos
class NoAutenticado(Exception):
    pass


@app.exception_handler(NoAutenticado)
async def _no_autenticado(request: Request, exc: NoAutenticado):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"status": "error", "message": "No autorizado"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def usuario_actual(request: Request, db: Session = Depends(get_db)) -> str:
    """Usuario de la sesión FIRMADA (ya no existe la cookie 'usuario' en texto plano)."""
    usuario = request.session.get("usuario")
    if not usuario or not db.query(UsuarioDB).filter(UsuarioDB.username == usuario).first():
        request.session.clear()
        raise NoAutenticado()
    return usuario


def admin_actual(usuario: str = Depends(usuario_actual)) -> str:
    if usuario not in ADMINS:
        raise HTTPException(status_code=403, detail="Solo administradores")
    return usuario


# Freno básico contra fuerza bruta en el login: 6 fallos en 10 minutos bloquean ese usuario+IP.
_fallos: dict = {}


def _clave_intentos(request: Request, usuario: str) -> tuple:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    return ip, usuario


def _bloqueado(clave) -> bool:
    cuenta, desde = _fallos.get(clave, (0, 0))
    if time.time() - desde > 600:
        _fallos.pop(clave, None)
        return False
    return cuenta >= 6


def _registrar_fallo(clave) -> None:
    cuenta, desde = _fallos.get(clave, (0, time.time()))
    _fallos[clave] = (cuenta + 1, desde)


# ------------------------------------------------------------------ autenticación
@app.get("/health")
async def health():
    return {"ok": True, "scheduler": core_engine.scheduler.running}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: int = 0, success: int = 0):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error, "success": success})


@app.get("/registro", response_class=HTMLResponse)
async def registro_page(request: Request, error: int = 0):
    return templates.TemplateResponse(request=request, name="registro.html", context={"error": error})


@app.post("/api/registro")
def procesar_registro(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = normalizar_cuenta(username)
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", user):
        return RedirectResponse("/registro?error=4", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/registro?error=3", status_code=303)
    # Solo se puede registrar quien tenga una cuenta de Spotify configurada (o si abres el registro a propósito).
    if not REGISTRO_ABIERTO and user not in obtener_credenciales() and user not in ADMINS:
        return RedirectResponse("/registro?error=2", status_code=303)
    if db.query(UsuarioDB).filter(UsuarioDB.username == user).first():
        return RedirectResponse("/registro?error=1", status_code=303)
    db.add(UsuarioDB(username=user, hashed_password=get_password_hash(password)))
    db.commit()
    return RedirectResponse("/login?success=1", status_code=303)


@app.post("/api/login")
def procesar_login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = normalizar_cuenta(username)
    clave = _clave_intentos(request, user)
    if _bloqueado(clave):
        return RedirectResponse("/login?error=2", status_code=303)
    db_user = db.query(UsuarioDB).filter(UsuarioDB.username == user).first()
    if not db_user or not verificar_password(password, db_user.hashed_password):
        _registrar_fallo(clave)
        return RedirectResponse("/login?error=1", status_code=303)
    _fallos.pop(clave, None)
    request.session.clear()  # sesión nueva en cada login
    request.session["usuario"] = user
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ conexión con Spotify
@app.get("/", response_class=HTMLResponse)
def index(request: Request, usuario: str = Depends(usuario_actual)):
    estado = tokens.estado_token(usuario)
    return templates.TemplateResponse(request=request, name="index.html", context={
        "usuario_actual": usuario.capitalize(),
        "conectado": estado["conectada"],
        "perfil_spotify": estado["perfil"],
        "es_admin": usuario in ADMINS,
    })


@app.get("/conectar-spotify")
def conectar_spotify(request: Request, cuenta: Optional[str] = None, next: str = "/",
                     usuario: str = Depends(usuario_actual)):
    """Un usuario conecta su propia cuenta; un administrador puede conectar cualquiera desde el panel /core."""
    cuenta = normalizar_cuenta(cuenta) or usuario
    if cuenta != usuario and usuario not in ADMINS:
        raise HTTPException(status_code=403, detail="No puedes conectar otra cuenta")
    if cuenta not in obtener_credenciales():
        return RedirectResponse(next if next in ("/", "/core") else "/", status_code=303)
    estado = secrets.token_urlsafe(24)
    request.session["oauth"] = {"state": estado, "cuenta": cuenta, "next": next if next in ("/", "/core") else "/"}
    return RedirectResponse(obtener_auth_manager(cuenta).get_authorize_url(state=estado))


@app.get("/callback")
def callback_spotify(request: Request, code: Optional[str] = None, state: Optional[str] = None,
                     usuario: str = Depends(usuario_actual)):
    datos = request.session.pop("oauth", None)
    destino = (datos or {}).get("next", "/")
    # El 'state' aleatorio evita que alguien enlace SU Spotify a TU sesión (CSRF del OAuth).
    if not datos or not state or not hmac.compare_digest(state, datos["state"]) or not code:
        return RedirectResponse(destino, status_code=303)
    cuenta = datos["cuenta"]
    try:
        info = obtener_auth_manager(cuenta).get_access_token(code, as_dict=True, check_cache=False)
        perfil = spotipy.Spotify(auth=info["access_token"], requests_timeout=15).current_user()
        tokens.guardar_token(cuenta, info, perfil=perfil.get("display_name") or perfil.get("id") or "")
    except Exception:
        logger.exception("Falló la conexión de %s con Spotify", cuenta)
    return RedirectResponse(destino, status_code=303)


# ------------------------------------------------------------------ creación manual
class CrearListasBody(BaseModel):
    nombres: str
    language: str = "es"
    id_extra: List[str] = []


class PlaylistRequest(BaseModel):
    artist_name: str
    language: str = "es"
    id_extra: List[str] = []


@app.post("/api/crear_listas_json")
def crear_listas_json(payload: CrearListasBody, usuario: str = Depends(usuario_actual)):
    if not tokens.obtener_access_token(usuario):
        return [{"status": "auth_required", "message": "Conecta Spotify primero."}]
    nombres = [n.strip() for n in payload.nombres.split(",") if n.strip()][:60]
    return crear_listas_por_nombres(
        nombres, language=payload.language, id_extra=payload.id_extra, cuenta=usuario,
        proveedor_token=lambda: tokens.obtener_access_token(usuario), imagen_loader=cargar_imagen,
    )


@app.post("/api/crear_playlist")
def crear_playlist(req: PlaylistRequest, usuario: str = Depends(usuario_actual)):
    if not tokens.obtener_access_token(usuario):
        return [{"status": "auth_required", "message": "Conecta Spotify primero."}]
    cliente = SpotifyClient(lambda: tokens.obtener_access_token(usuario), usuario)
    try:
        return [crear_playlist_para_artista(
            cliente, req.artist_name.strip(), language=req.language,
            id_extra=req.id_extra, imagen_loader=cargar_imagen)]
    except CuotaAgotada as e:
        return [{"status": "error", "message": str(e)}]


@app.post("/api/upload_image")
def upload_image(file: UploadFile = File(...), artista: str = Form(""), usuario: str = Depends(usuario_actual)):
    datos = file.file.read(8 * 1024 * 1024 + 1)
    if len(datos) > 8 * 1024 * 1024:
        return JSONResponse({"status": "error", "message": "La imagen supera 8 MB"}, status_code=413)
    try:
        jpeg = preparar_jpeg(datos)   # se guarda ya como JPEG cuadrado <= 250 KB, listo para Spotify
    except ValueError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    nombre = artista.strip() or Path(file.filename or "portada").stem
    guardar_imagen(nombre, file.filename or "portada.jpg", jpeg)
    return {"status": "success"}


# ------------------------------------------------------------------ panel Core (solo administradores)
@app.get("/core")
def renderizar_core(request: Request, usuario: str = Depends(usuario_actual)):
    if usuario not in ADMINS:
        return RedirectResponse("/", status_code=303)
    return FileResponse(BASE_DIR / "templates" / "core-dashboard.html")


@app.get("/api/cuentas")
def listar_cuentas(admin: str = Depends(admin_actual)):
    estados = tokens.estados_tokens()
    return [
        {"cuenta": nombre, "configurada": c["configurada"],
         "conectada": estados.get(nombre, {}).get("conectada", False),
         "perfil": estados.get(nombre, {}).get("perfil", "")}
        for nombre, c in obtener_credenciales().items()
    ]


# --- semillas
class SemillaBody(BaseModel):
    artistas: str = ""


class RenombrarBody(BaseModel):
    nuevo: str


class ImportarBody(BaseModel):
    semillas: dict


@app.get("/api/semillas")
def obtener_semillas(db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    return {s.genero: s.artistas or "" for s in db.query(SemillaDB).order_by(SemillaDB.genero)}


@app.put("/api/semillas/{genero}")
def guardar_semilla(genero: str, body: SemillaBody, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    genero = genero.strip()
    if not genero:
        raise HTTPException(400, "Género vacío")
    fila = db.get(SemillaDB, genero)
    if not fila:
        fila = SemillaDB(genero=genero)
        db.add(fila)
    fila.artistas = "\n".join(lineas_artistas(body.artistas))
    fila.actualizado = utcnow()
    db.commit()
    return {"status": "success"}


@app.delete("/api/semillas/{genero}")
def borrar_semilla(genero: str, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    fila = db.get(SemillaDB, genero)
    if fila:
        db.delete(fila)
        db.commit()
    return {"status": "success"}


@app.post("/api/semillas/{genero}/renombrar")
def renombrar_semilla(genero: str, body: RenombrarBody, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    nuevo = body.nuevo.strip()
    fila = db.get(SemillaDB, genero)
    if not fila or not nuevo:
        raise HTTPException(404, "Género no encontrado")
    if db.get(SemillaDB, nuevo):
        raise HTTPException(409, "Ya existe un género con ese nombre")
    db.add(SemillaDB(genero=nuevo, artistas=fila.artistas))
    db.query(CampanaDB).filter(CampanaDB.genero == genero).update({"genero": nuevo})
    db.delete(fila)
    db.commit()
    return {"status": "success"}


@app.post("/api/semillas/importar")
def importar_semillas(body: ImportarBody, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    """Migración única: sube lo que tenías en el localStorage del navegador (solo agrega los que faltan)."""
    nuevos = 0
    for genero, texto in body.semillas.items():
        genero = str(genero).strip()
        if genero and not db.get(SemillaDB, genero):
            db.add(SemillaDB(genero=genero, artistas="\n".join(lineas_artistas(str(texto)))))
            nuevos += 1
    db.commit()
    return {"status": "success", "importados": nuevos}


# --- campañas (cron diario)
class CampanaPayload(BaseModel):
    cuentas_objetivo: List[str]
    nombre_genero: str
    volumen: int = 40
    hora: str = "09:00"            # HH:MM, hora de Caracas, se repite todos los días
    pausa_seg: int = 10
    retener_dias: int = 0          # 0 = no borrar nunca
    idioma: str = "es"
    extras: List[str] = []


class ActivaBody(BaseModel):
    activa: bool


@app.post("/api/campanas")
def programar_campanas(payload: CampanaPayload, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    try:
        hora, minuto = parse_hora(payload.hora)
    except ValueError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    if not 1 <= payload.volumen <= 100:
        return JSONResponse({"status": "error", "message": "El volumen debe estar entre 1 y 100"}, status_code=400)
    semilla = db.get(SemillaDB, payload.nombre_genero)
    if not semilla or not lineas_artistas(semilla.artistas):
        return JSONResponse({"status": "error",
                             "message": "El género no existe o no tiene cantantes. Agrégalos en el Banco de Semillas."},
                            status_code=400)
    disponibles = obtener_credenciales()
    cuentas = [normalizar_cuenta(c) for c in payload.cuentas_objetivo]
    desconocidas = [c for c in cuentas if c not in disponibles]
    if not cuentas or desconocidas:
        return JSONResponse({"status": "error", "message": f"Cuentas no válidas: {', '.join(desconocidas) or 'ninguna'}"},
                            status_code=400)

    creadas = 0
    for cuenta in cuentas:
        c = db.query(CampanaDB).filter(
            CampanaDB.cuenta == cuenta, CampanaDB.genero == payload.nombre_genero,
            CampanaDB.hora == hora, CampanaDB.minuto == minuto,
        ).first()
        if not c:
            c = CampanaDB(cuenta=cuenta, genero=payload.nombre_genero, hora=hora, minuto=minuto, creada_en=utcnow())
            db.add(c)
        c.volumen = payload.volumen
        c.pausa_seg = max(1, min(payload.pausa_seg, 300))
        c.retener_dias = max(0, payload.retener_dias)
        c.idioma = payload.idioma if payload.idioma in ("es", "en", "pt", "de", "fr") else "es"
        c.extras = "\n".join(lineas_artistas("\n".join(payload.extras)))
        c.activa = True
        db.commit()
        db.refresh(c)
        core_engine.registrar_campana(c)
        creadas += 1
    return {"status": "success", "mensaje": f"Se programaron {creadas} campañas diarias a las {hora:02d}:{minuto:02d}."}


@app.post("/api/campanas/{campana_id}/ejecutar")
def ejecutar_ahora(campana_id: int, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    if not db.get(CampanaDB, campana_id):
        raise HTTPException(404, "Campaña no encontrada")
    core_engine.scheduler.add_job(
        core_engine.ejecutar_campana, args=[campana_id, True],
        id=f"manual_{campana_id}_{int(time.time())}", replace_existing=True,
    )
    return {"status": "success", "mensaje": "Ejecución iniciada. Mira el progreso en el panel."}


@app.post("/api/campanas/{campana_id}/activa")
def pausar_reanudar(campana_id: int, body: ActivaBody, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    c = db.get(CampanaDB, campana_id)
    if not c:
        raise HTTPException(404, "Campaña no encontrada")
    c.activa = body.activa
    db.commit()
    if c.activa:
        core_engine.registrar_campana(c)
    else:
        core_engine.quitar_campana(c.id)
    return {"status": "success"}


@app.delete("/api/tareas/{tarea_id}")
def eliminar_tarea(tarea_id: str, db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    """`camp_N` elimina la campaña; `ej_N` borra una fila del historial."""
    tipo, _, numero = tarea_id.partition("_")
    if not numero.isdigit():
        raise HTTPException(400, "ID inválido")
    if tipo == "camp":
        c = db.get(CampanaDB, int(numero))
        if not c:
            raise HTTPException(404, "Campaña no encontrada")
        core_engine.quitar_campana(c.id)
        db.delete(c)
        db.commit()
        return {"status": "success", "message": "Campaña eliminada"}
    if tipo == "ej":
        e = db.get(EjecucionDB, int(numero))
        if e:
            db.delete(e)
            db.commit()
        return {"status": "success", "message": "Historial borrado"}
    raise HTTPException(400, "ID inválido")


@app.get("/api/reloj")
def obtener_reloj(admin: str = Depends(admin_actual)):
    return core_engine.estado_reloj()


# --- ejecución externa (para el plan gratuito de Render, que "duerme" el servicio)
@app.api_route("/api/cron/tick", methods=["GET", "POST"])
def cron_tick(request: Request, key: str = ""):
    """Un pinger externo (cron-job.org, UptimeRobot...) puede llamarlo cada 5-15 minutos.

    Despierta el servicio y lanza las campañas de hoy que no se hayan ejecutado.
    Protegido con CRON_SECRET (cabecera X-Cron-Secret o parámetro ?key=).
    """
    if not CRON_SECRET:
        raise HTTPException(404, "No habilitado")
    entregado = request.headers.get("x-cron-secret") or key
    if not hmac.compare_digest(entregado.encode(), CRON_SECRET.encode()):
        raise HTTPException(403, "Clave inválida")
    return {"status": "success", "lanzadas": core_engine.ejecutar_pendientes()}


# ------------------------------------------------------------------ configuración y usuarios
@app.get("/api/config-info")
def obtener_configuracion(db: Session = Depends(get_db), admin: str = Depends(admin_actual)):
    estados = tokens.estados_tokens()
    cuentas = []
    for nombre, c in obtener_credenciales().items():
        cid = c["client_id"]
        est = estados.get(nombre, {})
        cuentas.append({
            "cuenta": nombre,
            "perfil_spotify": est.get("perfil") or ("Reconectar" if est.get("invalido") else "Sin conectar"),
            "env_id": c["env_id"],
            "env_secret": c["env_secret"],
            "client_id": f"{cid[:8]}..." if len(cid) > 8 else cid,
            "is_active": c["configurada"],
            "conectada": est.get("conectada", False),
        })
    return {"usuarios_registrados": [u.username for u in db.query(UsuarioDB).order_by(UsuarioDB.username)],
            "cuentas_spotify": cuentas}


@app.delete("/api/usuarios/{username_del}")
def eliminar_usuario(username_del: str, db: Session = Depends(get_db), usuario: str = Depends(admin_actual)):
    objetivo = normalizar_cuenta(username_del)
    if objetivo == usuario:
        return {"status": "error", "message": "No puedes eliminar tu propio usuario"}
    fila = db.query(UsuarioDB).filter(UsuarioDB.username == objetivo).first()
    if not fila:
        return {"status": "error", "message": "El usuario no existe"}
    db.delete(fila)
    db.commit()
    return {"status": "success", "message": "Usuario eliminado correctamente"}
