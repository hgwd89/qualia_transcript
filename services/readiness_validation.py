"""Read-only validation helpers used by the production readiness audit."""
from __future__ import annotations

import hashlib
import json
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from services.raw_snapshot_storage import read_raw_snapshot_batch


_OOXML_RULES = {
    "xlsx": {
        "main_part": "xl/workbook.xml",
        "main_root": "workbook",
        "main_content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    },
    "docx": {
        "main_part": "word/document.xml",
        "main_root": "document",
        "main_content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    },
}
_BACKUP_NAME_PREFIX = "qualia_backup_"
_BACKUP_TIMESTAMP_LENGTH = len("YYYYMMDDTHHMMSSZ")


def load_raw_text_snapshots(output_dir: Path) -> tuple[dict[int, list[dict]], list[dict]]:
    """Load immutable raw text snapshots keyed by transcription id.

    Chunk-manifest JSON files are accepted as metadata but do not count as the
    immutable text snapshot required for a completed transcription. Integer IDs
    are only a lookup index; ownership is verified separately against the current
    transcription generation before a snapshot can satisfy readiness.

    Directory enumeration and every JSON read share one pinned raw-directory
    identity. Unsafe directory/file replacement therefore becomes an explicit
    readiness defect instead of redirecting validation to different bytes.
    """
    by_transcription: dict[int, list[dict]] = defaultdict(list)
    invalid: list[dict] = []
    raw_dir = output_dir / "raw_transcripts"

    try:
        snapshot_batch = read_raw_snapshot_batch(output_dir)
    except Exception as exc:
        invalid.append({
            "path": str(raw_dir),
            "reason": f"{type(exc).__name__}: {exc}",
        })
        return by_transcription, invalid

    for snapshot_name, snapshot_bytes in snapshot_batch:
        display_path = raw_dir / snapshot_name
        try:
            payload = json.loads(snapshot_bytes.decode("utf-8"))
        except Exception as exc:
            invalid.append({
                "path": str(display_path),
                "reason": f"{type(exc).__name__}: {exc}",
            })
            continue

        if not isinstance(payload, dict):
            invalid.append({"path": str(display_path), "reason": "snapshot is not a JSON object"})
            continue

        transcription_id = payload.get("transcription_id")
        if not isinstance(transcription_id, int):
            invalid.append({"path": str(display_path), "reason": "transcription_id missing/non-integer"})
            continue

        text = payload.get("text")
        if isinstance(text, str):
            actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            expected_hash = str(payload.get("sha256") or "").strip()
            if expected_hash and expected_hash != actual_hash:
                invalid.append({"path": str(display_path), "reason": "sha256 does not match snapshot text"})
                continue
            by_transcription[transcription_id].append({
                "path": str(display_path),
                "payload": payload,
            })
            continue

        # Chunk manifests are legitimate metadata, but not a raw text snapshot.
        if isinstance(payload.get("chunks"), list) and "status" in payload:
            continue

        invalid.append({
            "path": str(display_path),
            "reason": "snapshot has neither raw text nor recognized chunk-manifest structure",
        })

    return by_transcription, invalid


def load_raw_snapshot_tombstone_names(connection) -> set[str]:
    """Return source snapshot filenames already bound to deleted stable owners.

    Databases created before the provenance ledger are compatible because a
    missing table simply means there are no tombstones yet. Once the table exists,
    however, read failures must propagate: silently treating an unreadable ledger
    as empty could let a retained predecessor snapshot be attributed to a reused
    integer transcription ID.
    """
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_snapshot_tombstones'"
    ).fetchone()
    if not exists:
        return set()
    rows = connection.execute(
        "SELECT snapshot_name FROM raw_snapshot_tombstones"
    ).fetchall()
    return {str(row[0]) for row in rows if row[0]}


def _parse_utc_datetime(value) -> datetime | None:
    """Parse SQLite/ISO timestamps as UTC without mutating legacy source data."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def raw_snapshot_matches_transcription_generation(payload: dict, transcription) -> bool:
    """Return True only when a snapshot belongs to this exact transcription run.

    Project deletion deliberately retains immutable raw snapshots while SQLite may
    later reuse integer primary keys. Therefore ``transcription_id`` alone is not
    an ownership identity. Existing snapshots already carry ``interview_id`` and
    ``created_at_utc``; a completed Transcription carries ``started_at`` and
    ``completed_at``. Requiring all of them to agree fences retained predecessor
    snapshots away from a later row that happens to reuse the same integer ID.

    Missing legacy generation metadata fails closed for ownership attribution: the
    snapshot remains preserved and valid historical source material, but it cannot
    satisfy readiness for a current completed transcription.
    """
    if not isinstance(payload, dict):
        return False
    try:
        transcription_id = int(transcription["id"])
        interview_id = int(transcription["interview_id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False

    if payload.get("transcription_id") != transcription_id:
        return False
    if payload.get("interview_id") != interview_id:
        return False

    created_at = _parse_utc_datetime(payload.get("created_at_utc"))
    try:
        started_value = transcription["started_at"]
        completed_value = transcription["completed_at"]
    except (KeyError, IndexError, TypeError):
        return False
    started_at = _parse_utc_datetime(started_value)
    completed_at = _parse_utc_datetime(completed_value)
    if created_at is None or started_at is None or completed_at is None:
        return False
    if completed_at < started_at:
        return False
    return started_at <= created_at <= completed_at


def missing_raw_snapshot_transcription_ids(
    transcriptions,
    by_transcription: dict[int, list[dict]],
    tombstoned_snapshot_names: set[str] | None = None,
) -> list[int]:
    """Return completed transcription IDs lacking a snapshot from their generation."""
    tombstoned = tombstoned_snapshot_names or set()
    missing: list[int] = []
    for row in transcriptions:
        try:
            transcription_id = int(row["id"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        candidates = by_transcription.get(transcription_id, [])
        if not any(
            Path(str(candidate.get("path") or "")).name not in tombstoned
            and raw_snapshot_matches_transcription_generation(
                candidate.get("payload") if isinstance(candidate, dict) else {},
                row,
            )
            for candidate in candidates
            if isinstance(candidate, dict)
        ):
            missing.append(transcription_id)
    return missing


def _xml_local_name(tag: object) -> str:
    text = str(tag or "")
    return text.rsplit("}", 1)[-1]


def _ooxml_xml_root(archive: zipfile.ZipFile, member: str) -> str:
    """Read only the first XML element so large document bodies stay streaming."""
    with archive.open(member, "r") as stream:
        for _, element in ET.iterparse(stream, events=("start",)):
            return _xml_local_name(element.tag)
    return ""


def _validate_ooxml_package(source, fmt: str) -> str | None:
    rule = _OOXML_RULES[fmt]
    main_part = str(rule["main_part"])
    try:
        with zipfile.ZipFile(source, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                return f"{fmt} OOXML package contains duplicate member paths"

            bad_member = archive.testzip()
            if bad_member is not None:
                return f"{fmt} OOXML package has a corrupt ZIP member: {bad_member}"

            required = {"[Content_Types].xml", "_rels/.rels", main_part}
            missing = sorted(required - set(names))
            if missing:
                return f"{fmt} OOXML package is missing required parts: {', '.join(missing)}"

            try:
                with archive.open("[Content_Types].xml", "r") as stream:
                    content_types_root = ET.parse(stream).getroot()
                with archive.open("_rels/.rels", "r") as stream:
                    relationships_root = ET.parse(stream).getroot()
                main_root = _ooxml_xml_root(archive, main_part)
            except (ET.ParseError, UnicodeError, ValueError) as exc:
                return f"{fmt} OOXML package contains invalid XML: {exc}"

            if _xml_local_name(content_types_root.tag) != "Types":
                return f"{fmt} OOXML content-types root is invalid"
            if _xml_local_name(relationships_root.tag) != "Relationships":
                return f"{fmt} OOXML relationships root is invalid"
            if main_root != str(rule["main_root"]):
                return f"{fmt} OOXML main part root is invalid: {main_root or '<missing>'}"

            expected_part_name = f"/{main_part}"
            expected_content_type = str(rule["main_content_type"])
            has_content_type = any(
                _xml_local_name(element.tag) == "Override"
                and str(element.attrib.get("PartName") or "") == expected_part_name
                and str(element.attrib.get("ContentType") or "") == expected_content_type
                for element in content_types_root
            )
            if not has_content_type:
                return f"{fmt} OOXML package does not declare the expected main content type"

            has_office_relationship = any(
                _xml_local_name(element.tag) == "Relationship"
                and str(element.attrib.get("Type") or "").endswith("/officeDocument")
                and str(element.attrib.get("Target") or "").lstrip("/") == main_part
                for element in relationships_root
            )
            if not has_office_relationship:
                return f"{fmt} OOXML package has no officeDocument relationship to {main_part}"
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError) as exc:
        return f"{fmt} is not a valid ZIP/OOXML container: {exc}"

    return None


def validate_generated_artifact_stream(stream: BinaryIO, file_format: str) -> str | None:
    """Validate one already-pinned/snapshotted artifact byte stream."""
    fmt = str(file_format or "").lower().strip()
    try:
        stream.seek(0)
    except (AttributeError, OSError, ValueError) as exc:
        return f"artifact stream is not seekable: {exc}"

    if fmt in _OOXML_RULES:
        return _validate_ooxml_package(stream, fmt)

    if fmt == "csv":
        try:
            stream.seek(0)
            chunk = stream.read(4096)
            if isinstance(chunk, str):
                chunk.encode("utf-8")
            else:
                bytes(chunk).decode("utf-8-sig")
        except UnicodeError as exc:
            return f"csv is not readable as UTF-8/UTF-8-SIG: {exc}"
        except (OSError, ValueError, TypeError) as exc:
            return f"csv cannot be read for structural validation: {exc}"
    return None


def validate_generated_artifact(path: Path, file_format: str) -> str | None:
    """Return an error reason when a registered deliverable is structurally invalid."""
    fmt = (file_format or path.suffix.lstrip(".")).lower().strip()
    try:
        with path.open("rb") as stream:
            return validate_generated_artifact_stream(stream, fmt)
    except OSError as exc:
        return f"artifact cannot be opened for structural validation: {exc}"


def _backup_recency_key(path: Path) -> tuple[str, int, str]:
    """Keep filename chronology, but use mtime to order same-second backups."""
    name = path.name
    timestamp_start = len(_BACKUP_NAME_PREFIX)
    timestamp_key = name[timestamp_start:timestamp_start + _BACKUP_TIMESTAMP_LENGTH]
    try:
        mtime_ns = int(path.stat().st_mtime_ns)
    except OSError:
        mtime_ns = -1
    return timestamp_key, mtime_ns, name


def validate_latest_backup(backup_dir: Path) -> tuple[Path | None, str | None]:
    """Validate the newest Qualia backup archive without modifying local data."""
    archives = list(backup_dir.glob("qualia_backup_*.zip")) if backup_dir.is_dir() else []
    if not archives:
        return None, None

    # Backup filenames encode UTC only to whole seconds. Multiple serialized
    # maintenance backups can still be published within one second, and their
    # label/UUID suffixes are not chronological. Preserve the filename timestamp
    # ordering across seconds, then use filesystem nanosecond mtime as the actual
    # publication-order tie breaker within the same encoded second.
    latest = max(archives, key=_backup_recency_key)
    try:
        from services.local_backup import validate_backup

        validate_backup(latest)
    except Exception as exc:
        return latest, f"{type(exc).__name__}: {exc}"
    return latest, None
