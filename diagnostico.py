"""Revisión rápida del sistema (reemplaza a test_variables.py).

Uso (en tu PC con el .env, o en la Shell de Render):

    python diagnostico.py                                   # revisa todo, no crea nada
    python diagnostico.py --crear "Bad Bunny" --cuenta miguel   # además crea UNA playlist de prueba
"""
import argparse
import sys

from auth_db import (
    CampanaDB, EjecucionDB, SemillaDB, SessionLocal, URL, UsuarioDB, init_db,
)
from spotify_utils import SpotifyClient, crear_playlist_para_artista, obtener_credenciales
from tokens import estados_tokens, obtener_access_token


def linea(ok, texto):
    print(("✅ " if ok else "❌ ") + texto)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crear", help="Nombre de un artista para crear UNA playlist de prueba")
    ap.add_argument("--cuenta", help="Cuenta con la que crear la playlist de prueba")
    args = ap.parse_args()
    todo_ok = True

    print("== Base de datos ==")
    try:
        init_db()
        db = SessionLocal()
        usuarios, semillas = db.query(UsuarioDB).count(), db.query(SemillaDB).count()
        campanas, ejecuciones = db.query(CampanaDB).count(), db.query(EjecucionDB).count()
        db.close()
        motor = "SQLite local (¡en Render debe ser Postgres!)" if URL.startswith("sqlite") else "Postgres"
        linea(not URL.startswith("sqlite"), f"Conectada: {motor}")
        print(f"   usuarios={usuarios} semillas={semillas} campañas={campanas} ejecuciones={ejecuciones}")
    except Exception as e:
        todo_ok = linea(False, f"No se pudo usar la base de datos: {e}")

    print("\n== Cuentas de Spotify (variables de entorno) ==")
    cuentas = obtener_credenciales()
    if not cuentas:
        todo_ok = linea(False, "No se encontró ninguna pareja NOMBRE_CLIENT_ID / NOMBRE_CLIENT_SECRET")
    tokens = estados_tokens()
    for nombre, c in cuentas.items():
        if not c["configurada"]:
            todo_ok = linea(False, f"{nombre}: falta {c['env_secret']}") and todo_ok
            continue
        est = tokens.get(nombre)
        if not est:
            linea(False, f"{nombre}: variables OK, pero SIN CONECTAR (conéctala desde /core → Configuración API)")
        elif est["invalido"]:
            linea(False, f"{nombre}: Spotify revocó el permiso, hay que reconectar")
        else:
            token = obtener_access_token(nombre)
            linea(bool(token), f"{nombre}: conectada ({est['perfil'] or 'sin nombre'}) - token {'vigente' if token else 'NO se pudo renovar'}")

    if args.crear:
        print("\n== Playlist de prueba ==")
        if not args.cuenta:
            sys.exit("Falta --cuenta")
        cliente = SpotifyClient(lambda: obtener_access_token(args.cuenta), args.cuenta)
        res = crear_playlist_para_artista(cliente, args.crear)
        todo_ok = linea(res["status"] == "success", str(res)) and todo_ok

    print("\n" + ("Todo en orden." if todo_ok else "Hay puntos por revisar (ver ❌ arriba)."))


if __name__ == "__main__":
    main()
