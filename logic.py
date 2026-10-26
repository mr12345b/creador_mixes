"""Lógica pura del proyecto (solo librería estándar).

Todo lo que no necesita Spotify, base de datos ni FastAPI vive aquí para poder
probarlo sin dependencias externas (ver tests/test_logic.py).
"""
import os
import random
import re
from datetime import datetime

# ---------------------------------------------------------------- estados
ESTADO_EN_CURSO = "En curso"
# Estados que dan por terminada la ejecución del día (no se reintenta solo).
ESTADOS_FINALES = {"Completada", "Parcial", "Pausada: cuota"}
# Si una ejecución falla (sin token, caída...) el cron la reintenta, hasta este máximo por día.
MAX_INTENTOS_DIA = 3

# Si el prefijo de las variables de entorno no coincide con el nombre de la cuenta.
# Ej.: tus variables son JESUS_CLIENT_ID pero el usuario de la web es "jesusv".
ALIAS_CUENTAS = {"jesus": "jesusv"}
PREFIJOS_IGNORADOS = {"SPOTIPY", "SPOTIFY"}


# ---------------------------------------------------------------- cuentas
def normalizar_cuenta(nombre) -> str:
    """Todas las cuentas se manejan en minúscula (evita el desajuste Music / music)."""
    return (nombre or "").strip().lower()


def descubrir_cuentas(environ=None) -> dict:
    """Detecta las cuentas de Spotify a partir de las variables de entorno.

    Busca pares <NOMBRE>_CLIENT_ID + <NOMBRE>_CLIENT_SECRET (o <NOMBRE>_SECRET_ID),
    sin importar mayúsculas. Para agregar una cuenta nueva basta con crear sus
    dos variables en Render: no hay que tocar el código.
    """
    env = os.environ if environ is None else environ
    valores = {}
    for clave, valor in env.items():
        if valor is None or not str(valor).strip():
            continue
        valores.setdefault(clave.upper(), str(valor).strip())

    cuentas = {}
    for clave, client_id in valores.items():
        if not clave.endswith("_CLIENT_ID"):
            continue
        prefijo = clave[: -len("_CLIENT_ID")]
        if not prefijo or prefijo in PREFIJOS_IGNORADOS:
            continue
        secreto = valores.get(f"{prefijo}_CLIENT_SECRET") or valores.get(f"{prefijo}_SECRET_ID") or ""
        nombre = ALIAS_CUENTAS.get(prefijo.lower(), prefijo.lower())
        cuentas[nombre] = {
            "client_id": client_id,
            "client_secret": secreto,
            "env_id": f"{prefijo}_CLIENT_ID",
            "env_secret": f"{prefijo}_CLIENT_SECRET",
            "configurada": bool(secreto),
        }
    return dict(sorted(cuentas.items()))


# ---------------------------------------------------------------- artistas
def lineas_artistas(texto) -> list:
    """Un artista por línea (así se guardan las semillas). Quita vacíos y repetidos."""
    vistos, resultado = set(), []
    for linea in str(texto or "").replace("\r", "").split("\n"):
        nombre = linea.strip()
        if nombre and nombre.lower() not in vistos:
            vistos.add(nombre.lower())
            resultado.append(nombre)
    return resultado


def elegir_artistas(candidatos, usados, volumen, rng=random):
    """Elige `volumen` artistas rotando: no repite hasta agotar la lista.

    - `usados`: claves en minúscula de los artistas ya usados en el ciclo actual.
    - Devuelve (seleccion, reinicio). Si `reinicio` es True se agotó la lista y
      hay que borrar los usados antes de registrar los nuevos (empieza un ciclo).
    """
    unicos = lineas_artistas("\n".join(candidatos))
    usados = {str(u).lower() for u in usados}
    pendientes = [a for a in unicos if a.lower() not in usados]
    rng.shuffle(pendientes)
    if len(pendientes) >= volumen:
        return pendientes[:volumen], False

    seleccion = list(pendientes)
    elegidos = {a.lower() for a in seleccion}
    resto = [a for a in unicos if a.lower() not in elegidos]
    rng.shuffle(resto)
    seleccion += resto[: max(0, volumen - len(seleccion))]
    return seleccion, True


# ---------------------------------------------------------------- canciones
def limpiar_id_track(entrada) -> str:
    """Acepta un ID, un URI (spotify:track:ID) o una URL y devuelve el ID de 22 caracteres."""
    if not entrada or str(entrada).strip().lower() == "none":
        return ""
    entrada = str(entrada).strip()
    m = re.search(r"track(?:/|:)([a-zA-Z0-9]{22})", entrada)
    if m:
        return m.group(1)
    if re.match(r"^[a-zA-Z0-9]{22}$", entrada):
        return entrada
    return ""


def uris_extra(entradas) -> list:
    """Convierte las entradas del usuario en URIs únicos de tracks."""
    uris = []
    for e in entradas or []:
        tid = limpiar_id_track(e)
        uri = f"spotify:track:{tid}" if tid else ""
        if uri and uri not in uris:
            uris.append(uri)
    return uris


def insertar_extras(uris, extras, inicio=6, paso=4) -> list:
    """Mete las canciones extra en la lista: posición 7, 11, 15... (índices 6, 10, 14...).

    Equivale a lo que hacía el código viejo con una llamada a Spotify por canción,
    pero se calcula en memoria y se envía todo junto. También quita de la lista
    base las que ya vienen como extra para no duplicarlas.
    """
    extras = list(dict.fromkeys(extras or []))
    base = [u for u in uris if u not in set(extras)]
    pos = inicio
    for extra in extras:
        base.insert(min(pos, len(base)), extra)
        pos += paso
    return base


# ---------------------------------------------------------------- textos
IDIOMAS = {
    "es": ("{a} Éxitos de {a} MIX Mejores canciones de {a}", "Mejores canciones de {a}: "),
    "en": ("{a} All Songs - Best of {a} MIX | {a} Greatest Hits", "Best songs by {a}: "),
    "pt": ("{a} As Melhores - {a} MIX Melhores músicas de {a}", "Melhores músicas de {a}: "),
    "de": ("{a}: Hits von {a} MIX - Die besten Lieder von {a}", "Beste Lieder von {a}: "),
    "fr": ("{a} Toutes les chansons - Best of {a} MIX | Meilleurs titres de {a}",
           "Meilleures chansons de {a}: "),
}


def nombre_playlist(artista: str, idioma: str = "es") -> str:
    return IDIOMAS.get(idioma, IDIOMAS["es"])[0].replace("{a}", artista)


def descripcion_playlist(artista: str, idioma: str, nombres_canciones) -> str:
    prefijo = IDIOMAS.get(idioma, IDIOMAS["es"])[1].replace("{a}", artista)
    return (prefijo + ", ".join(list(nombres_canciones)[:18]))[:300]


# ---------------------------------------------------------------- tiempo / cron
def parse_hora(texto) -> tuple:
    """'09:30' -> (9, 30). Lanza ValueError si no es válida."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(texto or ""))
    if not m:
        raise ValueError("Hora inválida, usa el formato HH:MM")
    hora, minuto = int(m.group(1)), int(m.group(2))
    if not (0 <= hora <= 23 and 0 <= minuto <= 59):
        raise ValueError("Hora fuera de rango")
    return hora, minuto


def debe_ejecutar(hora, minuto, ahora: datetime, creada_en, estados_hoy) -> bool:
    """¿Hay que lanzar hoy esta campaña? Lo usan el reinicio del servidor y el endpoint /api/cron/tick.

    - Solo si ya pasó la hora programada de hoy.
    - No si esa hora programada es anterior a la creación de la campaña
      (una campaña creada a las 15:00 para las 09:00 no se ejecuta "con retraso" hoy).
    - No si hoy ya hay una ejecución en curso o terminada, ni si ya se agotaron los intentos.
    `ahora` y `creada_en` deben venir en la misma zona horaria (o ambos sin zona).
    """
    programada = ahora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if ahora < programada:
        return False
    if creada_en is not None and programada < creada_en:
        return False
    estados_hoy = list(estados_hoy)
    if ESTADO_EN_CURSO in estados_hoy:
        return False
    if any(e in ESTADOS_FINALES for e in estados_hoy):
        return False
    return len(estados_hoy) < MAX_INTENTOS_DIA


def segundos_reintento(cabeceras, por_defecto: int = 5) -> int:
    """Lee Retry-After de las cabeceras de un error 429 de Spotify."""
    valor = None
    if cabeceras:
        try:
            valor = cabeceras.get("Retry-After") or cabeceras.get("retry-after")
        except Exception:
            valor = None
    try:
        return max(1, int(float(valor)))
    except (TypeError, ValueError):
        return por_defecto
