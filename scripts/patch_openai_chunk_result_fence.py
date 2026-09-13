from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    transcription = root / "services" / "transcription.py"
    safe_check = root / "scripts" / "check_safe.ps1"

    replace_once(
        transcription,
        '''                    raw_text = _extract_response_text(resp)\n                    text = raw_text.strip()\n                    if not text:\n                        raise RuntimeError("empty transcription text")\n\n                    raw_snapshot_path, _ = _write_raw_transcript_snapshot(\n''',
        '''                    raw_text = _extract_response_text(resp)\n                    text = raw_text.strip()\n                    if not text:\n                        raise RuntimeError("empty transcription text")\n\n                    # Each successful long-audio chunk publishes immutable raw\n                    # evidence and canonical Segment rows. Durable workers must\n                    # reserve the attempt before either publication so recovery\n                    # cannot clean the stale attempt and then have it reappear.\n                    if result_write_guard is not None:\n                        result_write_guard()\n                    elif lease_check is not None:\n                        lease_check()\n\n                    raw_snapshot_path, _ = _write_raw_transcript_snapshot(\n''',
        "chunk pre-write fence",
    )

    replace_once(
        transcription,
        '''                    if lease_check is not None:\n                        lease_check()\n                    db.session.commit()\n''',
        '''                    if result_write_guard is None and lease_check is not None:\n                        lease_check()\n                    db.session.commit()\n''',
        "chunk pre-commit lease fallback",
    )

    replace_once(
        safe_check,
        '''Write-Host "[PASS] production transcription dispatch fencing smoke checks passed."\n\nWrite-Host "[INFO] Running linked managed-storage guard smoke check..."\n''',
        '''Write-Host "[PASS] production transcription dispatch fencing smoke checks passed."\n\nWrite-Host "[INFO] Running OpenAI chunk result-write fencing smoke check..."\npython tests/smoke_openai_chunk_write_fencing.py\nif ($LASTEXITCODE -ne 0) {\n    Write-Host "[FAIL] OpenAI chunk result-write fencing smoke checks failed (exit code: $LASTEXITCODE)."\n    exit $LASTEXITCODE\n}\nWrite-Host "[PASS] OpenAI chunk result-write fencing smoke checks passed."\n\nWrite-Host "[INFO] Running linked managed-storage guard smoke check..."\n''',
        "safe gate wiring",
    )


if __name__ == "__main__":
    main()
