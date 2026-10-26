"""Tokens de Spotify guardados en la base de datos (reemplaza a tokens_bg.json y a la cookie de sesión).

El `refresh_token` es lo que permite que el cron cree listas sin que nadie inicie sesión.
Se renueva solo y sobrevive a reinicios y deploys porque vive en Postgres.
"""
import logging
import threading
import time
from collections import defaultdict

from auth_db import SessionLocal, TokenSpotifyDB, cifrar, descifrar, utcnow
from logic import normalizar_cuenta
from notificaciones import notificar
from spotify_utils import obtener_auth_manager

logger = logging.getLogger(__name__)

_candados = defaultdict(threading.Lock)   # un candado por cuenta: evita renovar el mismo token dos veces a la vez
MARGEN_SEG = 120                          # renovar si faltan menos de 2 minutos


def guardar_token(cuenta: str, info: dict, perfil: str = "") -> None:
    cuenta = normalizar_cuenta(cuenta)
    db = SessionLocal()
    try:
        fila = db.get(TokenSpotifyDB, cuenta)
        if not fila:
            fila = TokenSpotifyDB(cuenta=cuenta)
            db.add(fila)
        fila.access_token = cifrar(info["access_token"])
        # Spotify no siempre devuelve un refresh_token nuevo al renovar: se conserva el anterior.
        nuevo_refresh = info.get("refresh_token")
        if nuevo_refresh:
            fila.refresh_token = cifrar(nuevo_refresh)
        fila.expires_at = int(info.get("expires_at") or (time.time() + int(info.get("expires_in", 3600))))
        fila.scope = info.get("scope", fila.scope or "")
        if perfil:
            fila.perfil = perfil
        fila.invalido = False
        fila.actualizado = utcnow()
        db.commit()
    finally:
        db.close()


def estado_token(cuenta: str) -> dict:
    """Resumen para el panel: ¿está conectada la cuenta?"""
    db = SessionLocal()
    try:
        fila = db.get(TokenSpotifyDB, normalizar_cuenta(cuenta))
        if not fila:
            return {"conectada": False, "invalido": False, "perfil": ""}
        return {"conectada": not fila.invalido, "invalido": bool(fila.invalido), "perfil": fila.perfil or ""}
    finally:
        db.close()


def estados_tokens() -> dict:
    db = SessionLocal()
    try:
        return {
            f.cuenta: {"conectada": not f.invalido, "invalido": bool(f.invalido), "perfil": f.perfil or ""}
            for f in db.query(TokenSpotifyDB).all()
        }
    finally:
        db.close()


def obtener_access_token(cuenta: str):
    """Devuelve un access_token válido (renovándolo si hace falta) o None si hay que reconectar."""
    cuenta = normalizar_cuenta(cuenta)
    with _candados[cuenta]:
        db = SessionLocal()
        try:
            fila = db.get(TokenSpotifyDB, cuenta)
            if not fila or fila.invalido or not fila.refresh_token:
                return None
            access = descifrar(fila.access_token)
            refresh = descifrar(fila.refresh_token)
            expira = fila.expires_at or 0
        finally:
            db.close()

        if access and expira - time.time() > MARGEN_SEG:
            return access

        try:
            nuevo = obtener_auth_manager(cuenta).refresh_access_token(refresh)
        except Exception as e:
            texto = str(e)
            if "invalid_grant" in texto or "revoked" in texto.lower():
                _marcar_invalido(cuenta)
                logger.error("El permiso de %s fue revocado: hay que reconectar", cuenta)
                notificar(f"⚠️ La cuenta <b>{cuenta}</b> perdió el permiso de Spotify. Reconéctala en el panel.")
                return None
            logger.warning("No se pudo renovar el token de %s (error temporal): %s", cuenta, e)
            # Error temporal (red, 5xx): si el token actual aún no venció, se sigue usando.
            return access if access and expira - time.time() > 10 else None

        guardar_token(cuenta, nuevo)
        return nuevo["access_token"]


def _marcar_invalido(cuenta: str) -> None:
    db = SessionLocal()
    try:
        fila = db.get(TokenSpotifyDB, cuenta)
        if fila:
            fila.invalido = True
            db.commit()
    finally:
        db.close()
