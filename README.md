# Creador de Mixes — Gestor de Playlists + Cron diario

Crea playlists de mixes por cantante en varias cuentas de Spotify, a mano (panel `/`) o **automáticamente todos los días** (panel `/core`).

## Qué cambió en esta versión

| Antes | Ahora |
|---|---|
| La automatización corría **una sola vez** (`'date'`) y `intervalo_horas` no se usaba | **Cron diario** por campaña (cuenta + género + hora), guardado en Postgres y restaurado en cada arranque |
| Tokens en `tokens_bg.json` y tareas/estadísticas en memoria o archivo (se perdían en cada deploy) | Todo en **Postgres**: tokens, campañas, historial, contador, rotación, portadas |
| Cookie `usuario` en texto plano (cualquiera podía entrar poniendo `usuario=miguel`) | **Sesión firmada** (`SESSION_SECRET`), `/core` solo para administradores, freno de fuerza bruta en el login, `state` aleatorio en el OAuth |
| `SESSION_SECRET` con valor por defecto escrito en el código | Sin valor por defecto: si falta, se usa una clave temporal y se avisa en el log |
| 19 cuentas escritas a mano en el código | Se **detectan solas** desde las variables de entorno (`NOMBRE_CLIENT_ID` + `NOMBRE_CLIENT_SECRET`) |
| ~25-30 llamadas a Spotify por playlist | ~10 (sin `sp.track()` x18, sin `current_user()`, extras en una sola llamada) |
| Sin manejo de errores 429 | Espera `Retry-After`, reintenta, y si la cuota se agota **corta el lote limpiamente** y avisa |
| Elegía artistas al azar (podía repetir) | **Rotación**: no repite artistas hasta agotar la lista del género |
| Portadas subidas nunca se usaban (ruta equivocada) y se guardaban en disco/SQLite efímeros | Portadas en Postgres, convertidas a JPEG ≤250 KB; se aplican de verdad |
| Semillas (artistas por género) en el `localStorage` del navegador | En Postgres (se migran solas la primera vez que abras el panel) |
| `async def` con trabajo bloqueante (congelaba el servidor mientras se creaban listas) | Rutas síncronas en el threadpool |
| Dashboard con 7 de 19 cuentas fijas | Cuentas dinámicas + botón **Conectar/Reconectar** por cuenta |

Compatibilidad con Spotify (guía de migración feb-2026): búsqueda `limit=10` con paginación, `POST /me/playlists`, `POST /playlists/{id}/items`, sin `artist_top_tracks` (ya no existe). Si Spotify respondiera 404/403/405 en un endpoint nuevo, se usa automáticamente el anterior.

Archivos eliminados (código muerto): `Database.py` (copia exacta de `auth_db.py`), `s` (copia vieja de `spotify_utils.py`), `letras.py` (vacío), `imagenes/c.py`, `db_utils.py` (SQLite efímero), `test_variables.py` (reemplazado por `diagnostico.py`), `templates/canciones.html` y `static/canciones.css` (página huérfana: su ruta no existe aquí; vive en tu otro repo) y las dependencias `airtable-python-wrapper`, `lyricsgenius` y `psycopg` (no se usaban).

## Puesta en marcha en Render

1. **Sube estos archivos a tu repo** (reemplazan a los anteriores). El `.gitignore` evita que subas `.env` o `tokens_bg.json`.
2. **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT` — **un solo worker** (no uses `--workers 2` o los cron correrían duplicados).
3. **Variables de entorno** (ver `.env.example`):
   - `DATABASE_URL` (la de tu Postgres de Render), `SESSION_SECRET` (48+ caracteres aleatorios), `SPOTIPY_REDIRECT_URI`.
   - Una pareja `NOMBRE_CLIENT_ID` / `NOMBRE_CLIENT_SECRET` por cuenta. Las que ya tienes (`MIGUEL_CLIENT_ID`, …) siguen funcionando tal cual, también con el formato `NOMBRE_SECRET_ID`. `JESUS_*` se reconoce como la cuenta `jesusv`.
   - `ADMIN_USERS=miguel` (quién puede entrar a `/core`; por defecto `miguel`).
   - Opcionales: `CRON_SECRET`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TOKEN_ENC_KEY`.
4. **Reconecta cada cuenta UNA vez.** Los tokens que estaban en `tokens_bg.json` no se migran (el archivo no sobrevivía a los deploys). Entra a `/core` → *Configuración API* → botón **Conectar** de cada cuenta (inicia sesión en Spotify con esa cuenta cuando se abra el login; hay que aceptar los permisos). Se añadió el permiso `user-library-modify`, necesario solo para la limpieza opcional de listas viejas.
5. **Carga las semillas:** *Banco de Semillas* (si tenías géneros en el navegador, se importan solos la primera vez).
6. **Crea la campaña:** *Panel General* → *Nueva Campaña por Lotes*: elige cuentas, género, **listas por día (40)**, **hora diaria** y pausa entre listas.

## Prueba piloto (hazla antes de escalar a todas las cuentas)

1. Ejecuta `python diagnostico.py` (en la Shell de Render o en tu PC con el `.env`): revisa base de datos, cuentas y tokens.
2. Crea una campaña con **1 cuenta y volumen 3** y pulsa **▶ Ejecutar ahora**. Mira el progreso en vivo y el resultado en el historial.
3. Si va bien, sube a 40 con esa misma cuenta, observa si aparece algún `Pausada: cuota`, y recién entonces agrega las demás.

## Plan gratuito de Render (el servicio se duerme)

Un servicio dormido no ejecuta nada. Define `CRON_SECRET` y programa un pinger gratuito (cron-job.org, UptimeRobot…) que llame cada 5–10 minutos a:

```
https://TU-APP.onrender.com/api/cron/tick        con la cabecera   X-Cron-Secret: <tu CRON_SECRET>
```

Eso despierta el servicio y lanza las campañas de hoy que aún no se hayan ejecutado (es idempotente: nunca duplica). Si pagas un plan que no duerme, no hace falta.

## Cómo se comporta

- **Una ejecución = una campaña en un día.** Si falla (sin token, caída), se reintenta hasta 3 veces ese día; si termina (`Completada`, `Parcial` o `Pausada: cuota`) no se repite hasta mañana.
- **Reinicios:** al arrancar, las ejecuciones que quedaron "En curso" se marcan `Interrumpida` y se reintentan si aún corresponde.
- **Rotación:** cada cuenta+género recorre toda su lista antes de repetir un artista.
- **Limpieza opcional:** si en la campaña pones "Borrar listas con más de N días" (0 = nunca), antes de cada ejecución se eliminan las playlists que **creó el cron** hace más de N días. Úsalo para mantener cada cuenta en un número estable de listas.
- **Zona horaria:** las horas son de Caracas (UTC-4).
- **Cuotas de Spotify:** cada app tiene su propia cuota si cada cuenta usa su propio dashboard de desarrollador. Si Spotify responde 429 con espera larga o `QUOTA_EXCEEDED`, la ejecución se detiene (`Pausada: cuota`) y se avisa por Telegram.

## Seguridad

- Registro: solo pueden registrarse usuarios que coincidan con una cuenta de Spotify configurada (o con `ADMIN_USERS`), salvo que pongas `REGISTRO_ABIERTO=1`. Contraseña mínima de 8 caracteres.
- Opcional: genera una clave con `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` y ponla en `TOKEN_ENC_KEY` para que los tokens se guarden **cifrados** en Postgres. No la pierdas: sin ella habría que reconectar las cuentas.
- Si algún `CLIENT_SECRET` o token llegó a subirse a GitHub alguna vez, regénéralo en el dashboard de Spotify.

## Estructura

```
main.py              rutas web + API, sesiones, permisos
core_engine.py       cron (APScheduler), ejecución de campañas, historial
spotify_utils.py     OAuth, búsqueda, creación de playlists, reintentos 429
tokens.py            tokens de Spotify en BD (renovación automática)
auth_db.py           modelos y conexión (Postgres)
logic.py             lógica pura (rotación, extras, cuentas, cron) — probada
notificaciones.py    avisos por Telegram
diagnostico.py       revisión rápida del sistema
tests/               python -m unittest discover -s tests -v
templates/ static/   login, registro, panel principal, dashboard /core
```
