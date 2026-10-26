"""Todo lo que habla con Spotify: OAuth, búsqueda de canciones y creación de playlists.

Cambios frente a la versión anterior (según la guía de migración de Spotify, feb-2026):
- La búsqueda usa limit=10 y pagina (el máximo ya no es 20/50).
- Se eliminó `artist_top_tracks` (el endpoint dejó de existir).
- Las playlists se crean con POST /me/playlists y se llenan con POST /playlists/{id}/items,
  con respaldo automático a los endpoints antiguos si Spotify responde 404/403/405.
- Ya no se llama a sp.track() 18 veces por playlist: los nombres vienen en la búsqueda.
- Ya no se llama a current_user() por cada playlist (solo si hace falta el respaldo).
- Las canciones extra se insertan en memoria y se envían junto con el resto (1 llamada).
- Manejo de errores 429 con espera (Retry-After) y corte limpio si se agota la cuota.
"""
import base64
import io
import logging
import os
import random
import time

import requests
import spotipy
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from logic import (
    descripcion_playlist,
    descubrir_cuentas,
    insertar_extras,
    nombre_playlist,
    normalizar_cuenta,
    segundos_reintento,
    uris_extra,
)

try:  # python-dotenv es opcional en producción (Render ya inyecta las variables)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

SCOPE = "playlist-modify-public playlist-modify-private ugc-image-upload user-library-modify"
REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI", "https://playlist-e9xy.onrender.com/callback")

MAX_BYTES_PORTADA = 250_000      # Spotify acepta portadas JPEG de hasta 256 KB
MAX_PAGINAS_BUSQUEDA = 6         # 6 páginas x 10 resultados
MAX_REINTENTOS = 4
ESPERA_MAXIMA_429 = 300          # si Spotify pide esperar más de 5 min, cortamos la ejecución


class CuotaAgotada(Exception):
    """Spotify pide esperar demasiado (o la cuota diaria se agotó): hay que detener el lote."""

    def __init__(self, espera: int = 0, detalle: str = ""):
        super().__init__(f"Cuota de Spotify agotada (reintentar en {espera}s). {detalle}".strip())
        self.espera = espera
        self.detalle = detalle


# ------------------------------------------------------------------ cuentas / OAuth
def obtener_credenciales() -> dict:
    return descubrir_cuentas()


def obtener_auth_manager(cuenta: str) -> SpotifyOAuth:
    cuenta = normalizar_cuenta(cuenta)
    creds = obtener_credenciales().get(cuenta)
    if not creds or not creds["client_id"] or not creds["client_secret"]:
        raise ValueError(f"No hay credenciales configuradas en el entorno para: {cuenta}")
    return SpotifyOAuth(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=MemoryCacheHandler(),
        open_browser=False,
        show_dialog=True,
    )


# ------------------------------------------------------------------ imágenes
def preparar_jpeg(datos: bytes, max_bytes: int = MAX_BYTES_PORTADA) -> bytes:
    """Convierte cualquier imagen en un JPEG cuadrado <= 250 KB (requisito de Spotify)."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(datos))
        img.load()
    except Exception as e:
        raise ValueError("El archivo no es una imagen válida") from e
    img = img.convert("RGB")
    lado = min(img.size)
    izq, arr = (img.width - lado) // 2, (img.height - lado) // 2
    img = img.crop((izq, arr, izq + lado, arr + lado))
    for tam in (640, 512, 400, 300):
        reducida = img.copy()
        reducida.thumbnail((tam, tam))
        for calidad in (90, 80, 70, 60, 50):
            buf = io.BytesIO()
            reducida.save(buf, format="JPEG", quality=calidad, optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue()
    raise ValueError("No se pudo reducir la imagen por debajo de 250 KB")


# ------------------------------------------------------------------ cliente con reintentos
class SpotifyClient:
    """Envuelve spotipy: reintenta 429/5xx, cachea el user_id y renueva el token cuando hace falta."""

    def __init__(self, proveedor_token, cuenta: str = ""):
        self.proveedor_token = proveedor_token
        self.cuenta = cuenta
        self.token = None
        self.sp = None
        self._user_id = None
        self.refrescar()

    def refrescar(self) -> bool:
        """Pide el token vigente (la capa de tokens lo renueva si está por vencer)."""
        token = self.proveedor_token()
        if not token:
            return False
        if token != self.token:
            self.token = token
            self.sp = spotipy.Spotify(
                auth=token,
                requests_timeout=20,
                retries=2,
                status_retries=2,
                # 429 NO va aquí: lo manejamos nosotros para poder leer Retry-After.
                status_forcelist=(500, 502, 503, 504),
            )
        return True

    def llamar(self, funcion, *args, **kwargs):
        for intento in range(MAX_REINTENTOS + 1):
            try:
                return funcion(*args, **kwargs)
            except SpotifyException as e:
                if e.http_status == 429:
                    espera = segundos_reintento(getattr(e, "headers", None))
                    texto = str(e)
                    if "QUOTA_EXCEEDED" in texto or espera > ESPERA_MAXIMA_429 or intento >= MAX_REINTENTOS:
                        raise CuotaAgotada(espera, texto[:200]) from e
                    logger.warning("429 en %s: esperando %ss", self.cuenta, espera)
                    time.sleep(espera + random.uniform(0.5, 2))
                    continue
                if e.http_status in (500, 502, 503, 504) and intento < MAX_REINTENTOS:
                    time.sleep(2 ** (intento + 1))
                    continue
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if intento >= MAX_REINTENTOS:
                    raise
                time.sleep(2 ** (intento + 1))

    def user_id(self) -> str:
        if not self._user_id:
            self._user_id = self.llamar(self.sp.current_user)["id"]
        return self._user_id


# ------------------------------------------------------------------ operaciones sobre Spotify
def buscar_artista(cliente: SpotifyClient, nombre: str):
    res = cliente.llamar(cliente.sp.search, q=f"artist:{nombre}", type="artist", limit=5)
    items = res.get("artists", {}).get("items", [])
    if not items:
        return None
    for item in items:
        if item.get("name", "").strip().lower() == nombre.strip().lower():
            return item
    return items[0]


def obtener_canciones_artista(cliente: SpotifyClient, artista: dict, num_tracks: int = 35) -> list:
    """Devuelve [(uri, nombre), ...] con canciones donde participa el artista."""
    vistos, canciones = set(), []

    def agregar(track):
        if track.get("uri") and track["uri"] not in vistos and any(
            a.get("id") == artista["id"] for a in track.get("artists", [])
        ):
            vistos.add(track["uri"])
            canciones.append((track["uri"], track.get("name", "")))

    try:
        for consulta in (f'artist:"{artista["name"]}"', artista["name"]):
            for pagina in range(MAX_PAGINAS_BUSQUEDA):
                res = cliente.llamar(
                    cliente.sp.search, q=consulta, type="track", limit=10, offset=pagina * 10
                )
                items = res.get("tracks", {}).get("items", [])
                if not items:
                    break
                for track in items:
                    agregar(track)
                if len(canciones) >= num_tracks:
                    break
            if len(canciones) >= 10:
                break
    except CuotaAgotada:
        raise
    except Exception as e:
        logger.warning("Aviso buscador (%s): %s", artista["name"], e)

    if len(canciones) < 10:  # respaldo: discografía del artista
        try:
            albumes = cliente.llamar(
                cliente.sp.artist_albums, artista["id"], album_type="album,single", limit=10
            )
            for album in albumes.get("items", []):
                if len(canciones) >= num_tracks:
                    break
                pistas = cliente.llamar(cliente.sp.album_tracks, album["id"], limit=50)
                for track in pistas.get("items", []):
                    agregar(track)
        except CuotaAgotada:
            raise
        except Exception as e:
            logger.warning("Aviso discografía (%s): %s", artista["name"], e)

    return canciones[:num_tracks]


def crear_playlist(cliente: SpotifyClient, nombre: str, descripcion: str) -> dict:
    cuerpo = {"name": nombre, "public": True, "description": descripcion}
    try:
        return cliente.llamar(cliente.sp._post, "me/playlists", payload=cuerpo)
    except SpotifyException as e:
        if e.http_status not in (400, 403, 404, 405):
            raise
        logger.info("POST /me/playlists no disponible (%s), usando endpoint anterior", e.http_status)
    return cliente.llamar(
        cliente.sp.user_playlist_create,
        cliente.user_id(), nombre, public=True, description=descripcion,
    )


def agregar_canciones(cliente: SpotifyClient, playlist_id: str, uris: list) -> None:
    for i in range(0, len(uris), 100):
        bloque = uris[i:i + 100]
        try:
            cliente.llamar(cliente.sp._post, f"playlists/{playlist_id}/items", payload={"uris": bloque})
        except SpotifyException as e:
            if e.http_status not in (400, 403, 404, 405):
                raise
            cliente.llamar(cliente.sp.playlist_add_items, playlist_id, bloque)


def subir_portada(cliente: SpotifyClient, playlist_id: str, jpeg: bytes) -> None:
    imagen_b64 = base64.b64encode(jpeg).decode("utf-8")
    cliente.llamar(cliente.sp.playlist_upload_cover_image, playlist_id, imagen_b64)


def eliminar_playlist(cliente: SpotifyClient, playlist_id: str) -> None:
    """'Elimina' (deja de seguir) una playlist propia."""
    try:
        cliente.llamar(cliente.sp._delete, "me/library", args={"uris": f"spotify:playlist:{playlist_id}"})
    except SpotifyException as e:
        if e.http_status not in (400, 403, 404, 405):
            raise
        cliente.llamar(cliente.sp.current_user_unfollow_playlist, playlist_id)


def _portada(artista_buscado: str, artista: dict, imagen_loader):
    """Portada propia (subida desde el panel) o, si no hay, la foto del artista en Spotify."""
    if imagen_loader:
        for clave in (artista_buscado, artista.get("name", "")):
            if clave:
                datos = imagen_loader(clave)
                if datos:
                    return datos
    imagenes = artista.get("images") or []
    if imagenes:
        resp = requests.get(imagenes[0]["url"], timeout=15)
        if resp.status_code == 200:
            return preparar_jpeg(resp.content)
    return None


def crear_playlist_para_artista(cliente: SpotifyClient, nombre_artista: str, num_tracks: int = 35,
                                language: str = "es", id_extra=None, imagen_loader=None) -> dict:
    """Crea UNA playlist. Devuelve {"status": "success"|"error", ...}. Propaga CuotaAgotada."""
    if not cliente.refrescar():
        return {"status": "auth_required", "message": "Sesión expirada. Conecta Spotify de nuevo."}
    try:
        artista = buscar_artista(cliente, nombre_artista)
        if not artista:
            return {"status": "error", "definitivo": True,
                    "message": f"No se encontró el artista {nombre_artista}"}

        canciones = obtener_canciones_artista(cliente, artista, num_tracks)
        if not canciones:
            return {"status": "error", "definitivo": True, "message": "No se encontraron canciones."}

        nombre = nombre_playlist(artista["name"], language)
        descripcion = descripcion_playlist(artista["name"], language, [n for _, n in canciones])
        uris = insertar_extras([u for u, _ in canciones], uris_extra(id_extra))

        playlist = crear_playlist(cliente, nombre, descripcion)
        playlist_id = playlist["id"]
        agregar_canciones(cliente, playlist_id, uris)

        try:
            jpeg = _portada(nombre_artista, artista, imagen_loader)
            if jpeg:
                subir_portada(cliente, playlist_id, jpeg)
        except CuotaAgotada:
            raise
        except Exception as e:  # la portada nunca debe tumbar la playlist
            logger.warning("No se pudo subir la portada de %s: %s", artista["name"], e)

        return {
            "status": "success",
            "playlist_id": playlist_id,
            "playlist_name": nombre,
            "playlist_url": (playlist.get("external_urls") or {}).get("spotify", ""),
            "artista": artista["name"],
        }
    except CuotaAgotada:
        raise
    except SpotifyException as e:
        logger.error("Error de Spotify (%s): %s", nombre_artista, e)
        if e.http_status == 403 and "insufficient" in str(e.msg).lower():
            return {"status": "error",
                    "message": "Tu conexión con Spotify no tiene permisos suficientes. Reconecta y acepta todos los permisos."}
        return {"status": "error", "message": f"Error de Spotify ({e.http_status}): {e.msg}"}
    except Exception as e:
        logger.exception("Error inesperado creando la lista de %s", nombre_artista)
        return {"status": "error", "message": f"Error del sistema: {e}"}


def crear_listas_por_nombres(nombres, language="es", proveedor_token=None, id_extra=None,
                             imagen_loader=None, pausa: float = 1.5, cuenta: str = "") -> list:
    """Uso manual (formulario 'Masivo'): crea una playlist por cada nombre, con una pausa corta."""
    cliente = SpotifyClient(proveedor_token, cuenta)
    resultados = []
    nombres = [n.strip() for n in nombres if str(n).strip()]
    for i, nombre in enumerate(nombres):
        try:
            res = crear_playlist_para_artista(
                cliente, nombre, 50, language, id_extra, imagen_loader
            )
        except CuotaAgotada as e:
            resultados.append({"artista": nombre, "status": "error", "message": str(e),
                               "playlist_name": "", "playlist_url": ""})
            break
        resultados.append({
            "artista": nombre,
            "status": res["status"],
            "message": res.get("message", ""),
            "playlist_name": res.get("playlist_name", ""),
            "playlist_url": res.get("playlist_url", ""),
        })
        if res["status"] == "auth_required":
            break
        if i < len(nombres) - 1:
            time.sleep(pausa)
    return resultados
