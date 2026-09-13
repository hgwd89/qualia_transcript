from pathlib import Path

architecture = Path('docs/architecture.md')
text = architecture.read_text(encoding='utf-8')
anchor = "The download route streams from the already-opened handle and reconstructs HTTP metadata from its `fstat()` result."
insert = (
    "Transcription uses the same ancestry-pinned read boundary for uploaded media, but pathname-only decoders and providers are never handed the mutable managed upload path. "
    "`create_media_read_snapshot()` copies the exact already-opened media object into an owner-private temporary file outside `UPLOAD_DIR`, verifies source generation and byte size across the copy, and `services/transcription.py` passes only that private snapshot pathname to PyAV, OpenAI, and faster-whisper. "
    "Both transcription provider paths close the snapshot in function-level `finally` blocks, so success and failure remove the temporary copy.\n\n"
)
if insert.strip() not in text:
    assert text.count(anchor) == 1
    text = text.replace(anchor, insert + anchor, 1)
architecture.write_text(text, encoding='utf-8')

testing = Path('docs/testing.md')
text = testing.read_text(encoding='utf-8')
old_bullet = "- media-upload integrity, including rejection of linked/reparse interview storage paths"
new_bullet = (
    "- media-upload integrity, including rejection of linked/reparse interview storage paths and transcription input snapshotting from an ancestry-pinned managed handle; the required regression verifies exact uploaded bytes, a private snapshot pathname outside `UPLOAD_DIR`, stability after the managed ID pathname is replaced, and deterministic snapshot cleanup"
)
assert old_bullet in text or new_bullet in text
text = text.replace(old_bullet, new_bullet, 1)
anchor2 = "The backup service-boundary regression uses only temporary database/upload/output/backup/lock paths."
paragraph = (
    "The media-upload/transcription regression also locks the source contract to the implementation: both OpenAI and local-Whisper paths must call `create_media_read_snapshot(media)`, neither may call `get_media_full_path(media)`, and both must close the snapshot. This keeps pathname-based decoder/provider consumers isolated from later replacement of the managed upload path without invoking a provider during the safe check.\n\n"
)
if paragraph.strip() not in text:
    assert text.count(anchor2) == 1
    text = text.replace(anchor2, paragraph + anchor2, 1)
testing.write_text(text, encoding='utf-8')

print('docs patched')
