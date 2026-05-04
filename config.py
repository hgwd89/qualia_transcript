import os
from dotenv import load_dotenv

load_dotenv()

SERVICE_NAME      = os.getenv("SERVICE_NAME", "Qualia Transcript")
SERVICE_SLUG      = os.getenv("SERVICE_SLUG", "qualia_transcript")
SECRET_KEY        = os.getenv("SECRET_KEY", "dev-secret-key")
DATABASE_URI      = f"sqlite:///{SERVICE_SLUG}.db"
UPLOAD_DIR        = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_DIR        = os.path.join(os.path.dirname(__file__), "outputs")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
WHISPER_MODEL     = os.getenv("WHISPER_MODEL", "small")
TRANSCRIPTION_PROVIDER = os.getenv("TRANSCRIPTION_PROVIDER", "openai")
TRANSCRIPTION_FALLBACK_PROVIDER = os.getenv("TRANSCRIPTION_FALLBACK_PROVIDER", "")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
MAX_UPLOAD_BYTES  = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".webm"}
