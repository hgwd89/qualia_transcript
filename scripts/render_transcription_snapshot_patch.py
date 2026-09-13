from __future__ import annotations

import base64
from pathlib import Path


path = Path("services/transcription.py")
source = path.read_text(encoding="utf-8")

old_import = "from services.upload_manager import get_media_full_path"
new_import = "from services.upload_manager import create_media_read_snapshot"
if source.count(old_import) != 1:
    raise SystemExit(f"unexpected upload-manager import count: {source.count(old_import)}")
source = source.replace(old_import, new_import, 1)

old_open = """    try:\n        media = tr.media_file\n        full_path = get_media_full_path(media)\n"""
new_open = """    media_snapshot = None\n    try:\n        media = tr.media_file\n        media_snapshot = create_media_read_snapshot(media)\n        full_path = media_snapshot.full_path\n"""
if source.count(old_open) != 2:
    raise SystemExit(f"unexpected transcription media-open count: {source.count(old_open)}")
source = source.replace(old_open, new_open, 2)

old_error_tail = """    except Exception as e:\n        # Never let uncommitted Segment rows hitchhike on the error-state commit.\n        # Completed long-audio chunks were committed earlier and remain auditable.\n        db.session.rollback()\n        tr.status = \"error\"\n        tr.error_message = _sanitize_error_message(str(e))\n        db.session.commit()\n        raise\n"""
new_error_tail = old_error_tail + """    finally:\n        if media_snapshot is not None:\n            media_snapshot.close()\n"""
if source.count(old_error_tail) != 2:
    raise SystemExit(f"unexpected transcription error-tail count: {source.count(old_error_tail)}")
source = source.replace(old_error_tail, new_error_tail, 2)

checks = {
    "legacy managed pathname calls": source.count("get_media_full_path(media)"),
    "snapshot creation calls": source.count("create_media_read_snapshot(media)"),
    "snapshot close calls": source.count("media_snapshot.close()"),
}
if checks != {
    "legacy managed pathname calls": 0,
    "snapshot creation calls": 2,
    "snapshot close calls": 2,
}:
    raise SystemExit(f"rendered transcription contract mismatch: {checks}")

encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
print(f"TRANSCRIPTION_PATCH_B64={encoded}")
