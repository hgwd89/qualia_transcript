from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "routes/interviews.py",
    "from services.product_hint import lookup_product_hints, render_inline_hint\n",
    "from services.product_hint import lookup_product_hints, render_inline_hint\n"
    "from services.upload_manager import save_and_register_media\n",
)
replace_once(
    "routes/interviews.py",
    '''        file = request.files.get("audio_file")\n        if file and file.filename and _allowed(file.filename):\n            ext = os.path.splitext(secure_filename(file.filename))[1].lower()\n            stored = f"{uuid.uuid4().hex}{ext}"\n            save_dir = os.path.join(config.UPLOAD_DIR, str(interview.id))\n            os.makedirs(save_dir, exist_ok=True)\n            full_path = os.path.join(save_dir, stored)\n            file.save(full_path)\n            rel_path = os.path.join(str(interview.id), stored)\n\n            media = MediaFile(\n                interview_id=interview.id,\n                original_filename=file.filename,\n                stored_path=rel_path,\n                file_type="audio" if ext in {".mp3", ".m4a", ".wav", ".ogg", ".flac"} else "video",\n                mime_type=file.content_type,\n            )\n            db.session.add(media)\n\n        db.session.commit()\n''',
    '''        file = request.files.get("audio_file")\n        if file and file.filename and _allowed(file.filename):\n            save_and_register_media(\n                file,\n                interview,\n                original_filename=file.filename,\n                mime_type=file.content_type,\n            )\n        else:\n            db.session.commit()\n''',
)

replace_once(
    "services/transcription.py",
    "from models.setting import AppSetting\n",
    "from models.setting import AppSetting\n"
    "from services.upload_manager import get_media_full_path\n",
)

p = Path("services/transcription.py")
text = p.read_text(encoding="utf-8")
old = "        full_path = os.path.join(config.UPLOAD_DIR, media.stored_path)\n"
count = text.count(old)
if count != 2:
    raise SystemExit(f"expected two media path joins, found {count}")
p.write_text(text.replace(old, "        full_path = get_media_full_path(media)\n"), encoding="utf-8")
