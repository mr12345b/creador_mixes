"""Base de datos (Postgres en Render; SQLite local si no hay DATABASE_URL).

Aquí vive TODO lo que antes estaba en archivos o en memoria y se perdía en cada deploy:
usuarios, tokens de Spotify, semillas (artistas por género), campañas (cron),
historial de ejecuciones, rotación de artistas, playlists creadas e imágenes de portada.
"""
import logging
import os
from datetime import datetime, timezone

from passlib.context import CryptContext
from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Integer, LargeBinary, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ contraseñas
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verificar_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ------------------------------------------------------------------ conexión
URL = os.getenv("DATABASE_URL") or "sqlite:///./local.db"
if URL.startswith("sqlite"):
    logger.warning("DATABASE_URL no está definida: usando SQLite local (se borra en cada deploy de Render).")
if URL.startswith("postgres://"):  # Render a veces entrega 'postgres://'
    URL = URL.replace("postgres://", "postgresql://", 1)

if URL.startswith("sqlite"):
    engine = create_engine(URL, connect_args={"check_same_thread": False})
else:
    # Varias cuentas pueden estar ejecutándose a la vez (un hilo por cuenta): pool holgado.
    engine = create_engine(URL, pool_pre_ping=True, pool_recycle=300, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow() -> datetime:
    """Fecha/hora UTC sin zona (así se guardan todas las fechas)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ modelos
class UsuarioDB(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)


class TokenSpotifyDB(Base):
    """Token de cada cuenta de Spotify. Es lo que permite crear listas sin que nadie inicie sesión."""
    __tablename__ = "tokens_spotify"
    cuenta = Column(String, primary_key=True)
    access_token = Column(Text)
    refresh_token = Column(Text)
    expires_at = Column(BigInteger, default=0)
    scope = Column(Text, default="")
    perfil = Column(String, default="")        # nombre visible en Spotify (se guarda al conectar)
    invalido = Column(Boolean, default=False)  # True si Spotify revocó el permiso: hay que reconectar
    actualizado = Column(DateTime, default=utcnow)


class SemillaDB(Base):
    """Banco de semillas: un género y su lista de artistas (uno por línea)."""
    __tablename__ = "semillas"
    genero = Column(String, primary_key=True)
    artistas = Column(Text, default="")
    actualizado = Column(DateTime, default=utcnow)


class CampanaDB(Base):
    """Una campaña = 'esta cuenta crea N listas de este género todos los días a esta hora'."""
    __tablename__ = "campanas"
    id = Column(Integer, primary_key=True)
    cuenta = Column(String, index=True, nullable=False)
    genero = Column(String, nullable=False)
    volumen = Column(Integer, default=40)
    hora = Column(Integer, default=9)
    minuto = Column(Integer, default=0)
    pausa_seg = Column(Integer, default=10)      # pausa media entre playlist y playlist
    retener_dias = Column(Integer, default=0)    # 0 = no borrar nunca las listas viejas
    idioma = Column(String, default="es")
    extras = Column(Text, default="")            # IDs/URLs de canciones extra, una por línea
    activa = Column(Boolean, default=True)
    creada_en = Column(DateTime, default=utcnow)


class EjecucionDB(Base):
    """Historial: una fila por cada vez que una campaña corre."""
    __tablename__ = "ejecuciones"
    id = Column(Integer, primary_key=True)
    campana_id = Column(Integer, index=True)
    cuenta = Column(String)
    genero = Column(String)
    volumen = Column(Integer, default=0)   # cuántas se intentaron
    creadas = Column(Integer, default=0)
    fallidas = Column(Integer, default=0)
    estado = Column(String, default="En curso")
    detalle = Column(Text, default="")
    fecha = Column(Date, index=True)       # día (hora de Caracas)
    inicio = Column(DateTime, default=utcnow)
    fin = Column(DateTime)
    manual = Column(Boolean, default=False)


class ArtistaUsadoDB(Base):
    """Rotación: artistas ya usados por cuenta+género en el ciclo actual."""
    __tablename__ = "artistas_usados"
    id = Column(Integer, primary_key=True)
    cuenta = Column(String, index=True)
    genero = Column(String, index=True)
    artista_key = Column(String)
    usado_en = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("cuenta", "genero", "artista_key", name="uq_artista_usado"),)


class PlaylistCreadaDB(Base):
    """Playlists creadas por el cron (sirve para borrar las viejas si se configura retención)."""
    __tablename__ = "playlists_creadas"
    id = Column(Integer, primary_key=True)
    cuenta = Column(String, index=True)
    playlist_id = Column(String)
    artista = Column(String)
    creada_en = Column(DateTime, default=utcnow, index=True)


class ImagenDB(Base):
    """Portadas por artista (antes eran archivos en disco + SQLite, que Render borra en cada deploy)."""
    __tablename__ = "imagenes"
    id = Column(Integer, primary_key=True)
    artista_key = Column(String, unique=True, index=True)
    nombre = Column(String)
    contenido = Column(LargeBinary)
    creada = Column(DateTime, default=utcnow)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ------------------------------------------------------------------ cifrado opcional de tokens
def _fernet():
    clave = os.getenv("TOKEN_ENC_KEY", "").strip()
    if not clave:
        return None
    from cryptography.fernet import Fernet
    return Fernet(clave.encode())


def cifrar(texto: str) -> str:
    """Si defines TOKEN_ENC_KEY, los tokens se guardan cifrados en la base de datos."""
    f = _fernet()
    if not f or not texto:
        return texto or ""
    return "enc:" + f.encrypt(texto.encode()).decode()


def descifrar(texto: str) -> str:
    if not texto or not texto.startswith("enc:"):
        return texto or ""
    f = _fernet()
    if not f:
        raise RuntimeError("Hay tokens cifrados pero falta la variable TOKEN_ENC_KEY")
    return f.decrypt(texto[4:].encode()).decode()


# ------------------------------------------------------------------ imágenes
def clave_artista(nombre: str) -> str:
    return (nombre or "").strip().lower()


def guardar_imagen(artista: str, nombre_archivo: str, contenido: bytes) -> None:
    db = SessionLocal()
    try:
        clave = clave_artista(artista)
        fila = db.query(ImagenDB).filter(ImagenDB.artista_key == clave).first()
        if fila:
            fila.contenido, fila.nombre, fila.creada = contenido, nombre_archivo, utcnow()
        else:
            db.add(ImagenDB(artista_key=clave, nombre=nombre_archivo, contenido=contenido))
        db.commit()
    finally:
        db.close()


def cargar_imagen(artista: str):
    """Devuelve los bytes JPEG de la portada del artista o None. Es el `imagen_loader` de spotify_utils."""
    db = SessionLocal()
    try:
        fila = db.query(ImagenDB).filter(ImagenDB.artista_key == clave_artista(artista)).first()
        return bytes(fila.contenido) if fila and fila.contenido else None
    finally:
        db.close()
