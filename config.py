import os
from dotenv import load_dotenv

load_dotenv()

SERVICE_NAME      = os.getenv("SERVICE_NAME", "Qualia Transcript")
SERVICE_SLUG      = os.getenv("SERVICE_SLUG", "qualia_transcript")
SECRET_KEY        = os.getenv("SECRET_KEY", "dev-secret-key")
DATABASE_URI      = f"sqlite:///{SERVICE_SLUG}.db"
UPLOAD_DIR        = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_DIR        = os.path.join(os.path.dirname(__file__), "outputs")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
WHISPER_MODEL     = os.getenv("WHISPER_MODEL", "large-v3")
MAX_UPLOAD_BYTES  = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".webm"}
