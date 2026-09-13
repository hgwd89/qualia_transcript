"""Stable provenance ledger for immutable raw transcript snapshots.

Raw transcript JSON is source data and is deliberately retained when a project is
deleted. SQLite integer IDs can later be reused, so the deleted row IDs alone are
not a durable owner identity. This module records a non-FK tombstone inside the
same database transaction as project deletion. The source JSON itself is never
rewritten or deleted.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text


TABLE_NAME = "raw_snapshot_tombstones"


def ensure_raw_snapshot_tombstone_table(session) -> None:
    """Create the provenance ledger transactionally when deletion first needs it."""
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            snapshot_name TEXT PRIMARY KEY,
            snapshot_sha256 TEXT NOT NULL,
            owner_token TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            interview_id INTEGER NOT NULL,
            transcription_id INTEGER NOT NULL,
            transcription_started_at TEXT,
            transcription_completed_at TEXT,
            snapshot_created_at_utc TEXT,
            deleted_at_utc TEXT NOT NULL
        )
    """))


def stage_project_raw_snapshot_tombstones(
    session,
    project_id: int,
    output_dir: str | Path,
) -> int:
    """Persist stable owner tombstones before project rows are deleted.

    The caller must already own the serialized project-deletion transaction. Any
    exception aborts that transaction, so database row deletion can never commit
    without the provenance rows that were successfully staged alongside it.

    A filename already present in the ledger belongs permanently to its earlier
    deleted owner. This is important when SQLite later reuses the same integer IDs
    and even the same transcription timestamps: a subsequent deletion must never
    rebind that retained historical source to the replacement row.

    Every parseable retained JSON whose transcription/interview IDs currently
    resolve to the project being deleted is tombstoned, even when its timestamp is
    outside the current transcription generation. Such a file may be legacy or
    already ambiguous because of historical ID reuse; excluding it permanently is
    safer than allowing a later reused ID/generation window to reclassify it as a
    current snapshot.
    """
    project_id = int(project_id)
    ensure_raw_snapshot_tombstone_table(session)

    rows = session.execute(
        text(
            """
            SELECT tr.id, mf.interview_id, tr.started_at, tr.completed_at
            FROM transcriptions tr
            JOIN media_files mf ON mf.id=tr.media_file_id
            JOIN interviews i ON i.id=mf.interview_id
            WHERE i.project_id=:project_id
            ORDER BY tr.id
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    if not rows:
        return 0

    existing_names = {
        str(row[0])
        for row in session.execute(
            text(f"SELECT snapshot_name FROM {TABLE_NAME}")
        ).all()
        if row[0]
    }
    by_transcription = {int(row["id"]): row for row in rows}
    owner_tokens = {int(row["id"]): uuid4().hex for row in rows}
    raw_dir = Path(output_dir).resolve() / "raw_transcripts"
    if not raw_dir.is_dir():
        return 0

    deleted_at_utc = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for path in sorted(raw_dir.glob("*.json")):
        if path.name in existing_names or path.is_symlink():
            continue
        try:
            snapshot_bytes = path.read_bytes()
            payload = json.loads(snapshot_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # Malformed source remains untouched. Readiness reports it as invalid;
            # without trustworthy owner IDs it cannot be safely attributed here.
            continue
        if not isinstance(payload, dict):
            continue
        transcription_id = payload.get("transcription_id")
        interview_id = payload.get("interview_id")
        if (
            not isinstance(transcription_id, int)
            or isinstance(transcription_id, bool)
            or not isinstance(interview_id, int)
            or isinstance(interview_id, bool)
        ):
            continue
        row = by_transcription.get(int(transcription_id))
        if row is None or int(interview_id) != int(row["interview_id"]):
            continue

        session.execute(
            text(
                f"""
                INSERT INTO {TABLE_NAME} (
                    snapshot_name,
                    snapshot_sha256,
                    owner_token,
                    project_id,
                    interview_id,
                    transcription_id,
                    transcription_started_at,
                    transcription_completed_at,
                    snapshot_created_at_utc,
                    deleted_at_utc
                ) VALUES (
                    :snapshot_name,
                    :snapshot_sha256,
                    :owner_token,
                    :project_id,
                    :interview_id,
                    :transcription_id,
                    :transcription_started_at,
                    :transcription_completed_at,
                    :snapshot_created_at_utc,
                    :deleted_at_utc
                )
                """
            ),
            {
                "snapshot_name": path.name,
                "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
                "owner_token": owner_tokens[int(transcription_id)],
                "project_id": project_id,
                "interview_id": int(row["interview_id"]),
                "transcription_id": int(transcription_id),
                "transcription_started_at": str(row["started_at"] or ""),
                "transcription_completed_at": str(row["completed_at"] or ""),
                "snapshot_created_at_utc": str(payload.get("created_at_utc") or ""),
                "deleted_at_utc": deleted_at_utc,
            },
        )
        existing_names.add(path.name)
        inserted += 1
    return inserted
