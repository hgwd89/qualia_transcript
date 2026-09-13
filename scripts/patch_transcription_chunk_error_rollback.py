from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


root = Path(__file__).resolve().parents[1]
transcription = root / "services" / "transcription.py"
test = root / "tests" / "smoke_transcription_state_fencing.py"

replace_once(
    transcription,
    '''                except Exception as chunk_error:\n                    err_type = type(chunk_error).__name__\n''',
    '''                except Exception as chunk_error:\n                    # A failed chunk can leave pending Segment rows or a failed\n                    # transaction. Discard those rows and release any earlier\n                    # reservation before reacquiring ownership for the error manifest.\n                    db.session.rollback()\n                    err_type = type(chunk_error).__name__\n''',
    "chunk error rollback",
)

replace_once(
    test,
    '''        chunk_error_start >= 0\n        and chunk_error_block.find("result_write_guard()") >= 0\n        and chunk_error_block.find("result_write_guard()")\n        < chunk_error_block.find("manifest_path = _write_chunk_manifest"),\n''',
    '''        chunk_error_start >= 0\n        and chunk_error_block.find("db.session.rollback()") >= 0\n        and chunk_error_block.find("result_write_guard()") >= 0\n        and chunk_error_block.find("db.session.rollback()")\n        < chunk_error_block.find("result_write_guard()")\n        < chunk_error_block.find("manifest_path = _write_chunk_manifest"),\n''',
    "chunk error regression",
)
