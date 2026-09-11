"""Read-only validation helpers used by the production readiness audit."""
from __future__ import annotations

import hashlib
import json
import zipfile
from collections import defaultdict
from pathlib import Path


def load_raw_text_snapshots(output_dir: Path) -> tuple[dict[int, list[dict]], list[dict]]:
    """Load immutable raw text snapshots keyed by transcription id.

    Chunk-manifest JSON files are accepted as metadata but do not count as the
    immutable text snapshot required for a completed transcription.
    """
    by_transcription: dict[int, list[dict]] = defaultdict(list)
    invalid: list[dict] = []
    raw_dir = output_dir / "raw_transcripts"
    if not raw_dir.is_dir():
        return by_transcription, invalid

    for path in sorted(raw_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            invalid.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
            continue

        if not isinstance(payload, dict):
            invalid.append({"path": str(path), "reason": "snapshot is not a JSON object"})
            continue

        transcription_id = payload.get("transcription_id")
        if not isinstance(transcription_id, int):
            invalid.append({"path": str(path), "reason": "transcription_id missing/non-integer"})
            continue

        text = payload.get("text")
        if isinstance(text, str):
            actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            expected_hash = str(payload.get("sha256") or "").strip()
            if expected_hash and expected_hash != actual_hash:
                invalid.append({"path": str(path), "reason": "sha256 does not match snapshot text"})
                continue
            by_transcription[transcription_id].append({"path": str(path), "payload": payload})
            continue

        # Chunk manifests are legitimate metadata, but not a raw text snapshot.
        if isinstance(payload.get("chunks"), list) and "status" in payload:
            continue

        invalid.append({
            "path": str(path),
            "reason": "snapshot has neither raw text nor recognized chunk-manifest structure",
        })

    return by_transcription, invalid


def validate_generated_artifact(path: Path, file_format: str) -> str | None:
    """Return an error reason when a registered deliverable is structurally invalid."""
    fmt = (file_format or path.suffix.lstrip(".")).lower().strip()
    if fmt in {"xlsx", "docx"}:
        if not zipfile.is_zipfile(path):
            return f"{fmt} is not a valid ZIP/OOXML container"
        return None

    if fmt == "csv":
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                handle.read(4096)
        except UnicodeError as exc:
            return f"csv is not readable as UTF-8/UTF-8-SIG: {exc}"
    return None


def validate_latest_backup(backup_dir: Path) -> tuple[Path | None, str | None]:
    """Validate the newest Qualia backup archive without modifying local data."""
    archives = sorted(backup_dir.glob("qualia_backup_*.zip")) if backup_dir.is_dir() else []
    if not archives:
        return None, None

    latest = archives[-1]
    try:
        from services.local_backup import validate_backup

        validate_backup(latest)
    except Exception as exc:
        return latest, f"{type(exc).__name__}: {exc}"
    return latest, None
