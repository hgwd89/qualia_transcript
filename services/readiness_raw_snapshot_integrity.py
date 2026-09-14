from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from services.raw_snapshot_storage import read_raw_snapshot_batch


@dataclass(frozen=True)
class RawSnapshotReadinessState:
    by_transcription: dict[int, list[dict]]
    invalid_snapshots: list[dict]
    tombstoned_names: set[str]
    tombstone_integrity_issues: list[dict]
    unproven_text_snapshots: list[dict]


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _load_tombstone_hashes(connection) -> tuple[dict[str, str], list[dict]]:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_snapshot_tombstones'"
    ).fetchone()
    if table_exists is None:
        return {}, []

    columns = {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in connection.execute("PRAGMA table_info(raw_snapshot_tombstones)").fetchall()
    }
    required = {"snapshot_name", "snapshot_sha256"}
    if not required.issubset(columns):
        return {}, [{
            "reason": "raw_snapshot_tombstones schema is missing provenance hash columns",
            "missing_columns": sorted(required - columns),
        }]

    hashes: dict[str, str] = {}
    issues: list[dict] = []
    for row in connection.execute(
        "SELECT snapshot_name, snapshot_sha256 FROM raw_snapshot_tombstones ORDER BY snapshot_name"
    ).fetchall():
        name = str(row["snapshot_name"] or "").strip()
        expected = str(row["snapshot_sha256"] or "").strip().lower()
        if not name:
            issues.append({"reason": "tombstone snapshot_name is empty"})
            continue
        if name in hashes:
            issues.append({"filename": name, "reason": "duplicate tombstone snapshot_name"})
            continue
        if not _valid_sha256(expected):
            issues.append({
                "filename": name,
                "reason": "tombstone snapshot_sha256 is missing or invalid",
            })
        hashes[name] = expected
    return hashes, issues


def load_raw_snapshot_readiness_state(connection, output_dir: Path) -> RawSnapshotReadinessState:
    """Validate active and tombstoned raw evidence from one pinned directory batch.

    The raw directory is enumerated/read once through ``read_raw_snapshot_batch``.
    Active text self-hashes and DB-persisted tombstone full-file hashes are then
    evaluated against those exact bytes so readiness cannot combine evidence from
    different pathname generations.
    """
    by_transcription: dict[int, list[dict]] = defaultdict(list)
    invalid: list[dict] = []
    unproven: list[dict] = []
    tombstone_hashes, tombstone_issues = _load_tombstone_hashes(connection)
    tombstoned_names = set(tombstone_hashes)

    try:
        snapshots = read_raw_snapshot_batch(output_dir)
    except (OSError, ValueError) as exc:
        return RawSnapshotReadinessState(
            by_transcription={},
            invalid_snapshots=[{
                "path": str(Path(output_dir) / "raw_transcripts"),
                "reason": f"{type(exc).__name__}: {exc}",
            }],
            tombstoned_names=tombstoned_names,
            tombstone_integrity_issues=tombstone_issues,
            unproven_text_snapshots=[],
        )

    bytes_by_name = {name: snapshot_bytes for name, snapshot_bytes in snapshots}
    for name, expected in tombstone_hashes.items():
        snapshot_bytes = bytes_by_name.get(name)
        if snapshot_bytes is None:
            tombstone_issues.append({
                "filename": name,
                "reason": "tombstoned retained raw snapshot is missing",
            })
            continue
        if _valid_sha256(expected):
            actual = hashlib.sha256(snapshot_bytes).hexdigest()
            if actual != expected:
                tombstone_issues.append({
                    "filename": name,
                    "reason": "tombstoned retained raw snapshot no longer matches recorded SHA-256",
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                })

    for filename, snapshot_bytes in snapshots:
        path = Path(output_dir) / "raw_transcripts" / filename
        try:
            payload = json.loads(snapshot_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("raw snapshot root is not a JSON object")
            if "text" not in payload:
                # Chunk manifests are metadata rather than immutable raw-text evidence.
                continue

            text = str(payload.get("text") or "")
            expected_hash = str(payload.get("sha256") or "").strip().lower()
            hash_verified = False
            if expected_hash:
                if not _valid_sha256(expected_hash):
                    raise ValueError("raw snapshot sha256 metadata is invalid")
                actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if actual_hash != expected_hash:
                    raise ValueError("raw snapshot sha256 mismatch")
                hash_verified = True

            transcription_id = payload.get("transcription_id")
            if (
                not isinstance(transcription_id, int)
                or isinstance(transcription_id, bool)
            ):
                raise ValueError("raw snapshot transcription_id missing/non-integer")
            interview_id = payload.get("interview_id")
            if (
                not isinstance(interview_id, int)
                or isinstance(interview_id, bool)
            ):
                interview_id = None
            created_at = str(payload.get("created_at_utc") or "").strip() or None
            candidate = {
                "path": str(path),
                "filename": filename,
                "payload": payload,
                "interview_id": interview_id,
                "created_at_utc": created_at,
                "hash_verified": hash_verified,
            }
            by_transcription[int(transcription_id)].append(candidate)

            # A tombstone's DB-persisted full-file hash is stronger than the
            # payload self-hash. Do not warn about a missing internal hash when
            # the retained historical file is already externally bound in SQLite.
            if not hash_verified and filename not in tombstoned_names:
                unproven.append({
                    "path": str(path),
                    "filename": filename,
                    "transcription_id": int(transcription_id),
                    "reason": "raw text snapshot has no recorded SHA-256",
                })
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            invalid.append({"path": str(path), "reason": str(exc)})

    return RawSnapshotReadinessState(
        by_transcription=dict(by_transcription),
        invalid_snapshots=invalid,
        tombstoned_names=tombstoned_names,
        tombstone_integrity_issues=tombstone_issues,
        unproven_text_snapshots=unproven,
    )
