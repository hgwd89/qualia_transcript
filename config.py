import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
SERVICE_NAME = os.getenv("SERVICE_NAME", "Qualia Transcript")
SERVICE_SLUG = os.getenv("SERVICE_SLUG", "qualia_transcript")
# Never fall back to a known/predictable session secret. For persistent sessions,
# set SECRET_KEY in .env; otherwise a process-local random key is generated.
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
DATABASE_PATH = os.path.join(INSTANCE_DIR, f"{SERVICE_SLUG}.db")
# Flask-SQLAlchemy resolves relative sqlite:/// URLs under Flask's instance path.
# Use the equivalent absolute path explicitly so backup/readiness/restore tooling
# and the application always refer to the exact same database file.
DATABASE_URI = f"sqlite:///{Path(DATABASE_PATH).as_posix()}"
RUNTIME_LOCK_PATH = os.getenv(
    "QUALIA_RUNTIME_LOCK_PATH",
    os.path.join(INSTANCE_DIR, "runtime.lock"),
)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "5000"))
APP_DEBUG = _env_bool("APP_DEBUG", False)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PRODUCT_HINT_PROVIDER = os.getenv("PRODUCT_HINT_PROVIDER", "rakuten")
RAKUTEN_APPLICATION_ID = os.getenv("RAKUTEN_APPLICATION_ID", "")
RAKUTEN_ACCESS_KEY = os.getenv("RAKUTEN_ACCESS_KEY", "")
RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
TRANSCRIPTION_PROVIDER = os.getenv("TRANSCRIPTION_PROVIDER", "openai")
TRANSCRIPTION_FALLBACK_PROVIDER = os.getenv("TRANSCRIPTION_FALLBACK_PROVIDER", "")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe-diarize")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".webm"}
