import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def write_snapshot(
    path: Path,
    *,
    transcription_id: int,
    interview_id: int,
    created_at_utc: str,
    text: str | None = None,
    manifest: bool = False,
) -> bytes:
    payload = {
        "transcription_id": int(transcription_id),
        "interview_id": int(interview_id),
        "created_at_utc": created_at_utc,
    }
    if manifest:
        payload.update({"status": "complete", "chunks": [{"index": 0}]})
    else:
        payload["text"] = text or ""
        payload["sha256"] = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path.read_bytes()


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original = {
        "BASE_DIR": config.BASE_DIR,
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_raw_snapshot_provenance_") as tmp:
        root = Path(tmp)
        db_path = root / "provenance.db"
        config.BASE_DIR = str(root)
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")

        try:
            from sqlalchemy import event

            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile, Transcription
            from models.project import Project
            from services.project_deletion import delete_project
            from services.readiness_validation import (
                load_raw_snapshot_tombstone_names,
                load_raw_text_snapshots,
                missing_raw_snapshot_transcription_ids,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                started = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
                completed = datetime(2026, 9, 13, 0, 10, tzinfo=timezone.utc)

                project = Project(name="Raw provenance owner")
                db.session.add(project)
                db.session.flush()
                project_id = int(project.id)

                interview = Interview(project_id=project_id)
                db.session.add(interview)
                db.session.flush()
                interview_id = int(interview.id)

                media = MediaFile(
                    interview_id=interview_id,
                    original_filename="source.wav",
                    stored_path=f"{interview_id}/source.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(media)
                db.session.flush()
                media_id = int(media.id)

                transcription = Transcription(
                    media_file_id=media_id,
                    whisper_model="test",
                    language="ja",
                    status="done",
                    started_at=started,
                    completed_at=completed,
                )
                db.session.add(transcription)
                db.session.commit()
                transcription_id = int(transcription.id)

                raw_dir = Path(config.OUTPUT_DIR) / "raw_transcripts"
                raw_snapshot = raw_dir / f"transcription_{transcription_id}_source.json"
                raw_manifest = raw_dir / f"transcription_{transcription_id}_manifest.json"
                legacy_snapshot = raw_dir / f"transcription_{transcription_id}_legacy.json"
                snapshot_bytes = write_snapshot(
                    raw_snapshot,
                    transcription_id=transcription_id,
                    interview_id=interview_id,
                    created_at_utc="2026-09-13T00:05:00+00:00",
                    text="immutable original source",
                )
                manifest_bytes = write_snapshot(
                    raw_manifest,
                    transcription_id=transcription_id,
                    interview_id=interview_id,
                    created_at_utc="2026-09-13T00:06:00+00:00",
                    manifest=True,
                )
                legacy_bytes = write_snapshot(
                    legacy_snapshot,
                    transcription_id=transcription_id,
                    interview_id=interview_id,
                    created_at_utc="2026-09-12T23:55:00+00:00",
                    text="legacy source outside current generation",
                )

                delete_project(project)

                con = sqlite3.connect(db_path)
                con.row_factory = sqlite3.Row
                try:
                    tombstones = con.execute(
                        """
                        SELECT snapshot_name, snapshot_sha256, owner_token,
                               project_id, interview_id, transcription_id
                        FROM raw_snapshot_tombstones
                        ORDER BY snapshot_name
                        """
                    ).fetchall()
                finally:
                    con.close()

                tombstone_by_name = {str(row["snapshot_name"]): row for row in tombstones}
                snapshot_tombstone = tombstone_by_name.get(raw_snapshot.name)
                manifest_tombstone = tombstone_by_name.get(raw_manifest.name)
                legacy_tombstone = tombstone_by_name.get(legacy_snapshot.name)
                failures += check(
                    "project deletion retains immutable raw source bytes",
                    raw_snapshot.read_bytes() == snapshot_bytes
                    and raw_manifest.read_bytes() == manifest_bytes
                    and legacy_snapshot.read_bytes() == legacy_bytes,
                )
                failures += check(
                    "project deletion tombstones current, manifest, and legacy out-of-generation source",
                    snapshot_tombstone is not None
                    and manifest_tombstone is not None
                    and legacy_tombstone is not None
                    and snapshot_tombstone["owner_token"]
                    == manifest_tombstone["owner_token"]
                    == legacy_tombstone["owner_token"]
                    and int(snapshot_tombstone["project_id"]) == project_id
                    and int(snapshot_tombstone["interview_id"]) == interview_id
                    and int(snapshot_tombstone["transcription_id"]) == transcription_id
                    and snapshot_tombstone["snapshot_sha256"]
                    == hashlib.sha256(snapshot_bytes).hexdigest()
                    and manifest_tombstone["snapshot_sha256"]
                    == hashlib.sha256(manifest_bytes).hexdigest()
                    and legacy_tombstone["snapshot_sha256"]
                    == hashlib.sha256(legacy_bytes).hexdigest(),
                    str([dict(row) for row in tombstones]),
                )

                replacement_project = Project(name="Replacement owner")
                db.session.add(replacement_project)
                db.session.flush()
                replacement_project_id = int(replacement_project.id)
                replacement_interview = Interview(project_id=replacement_project_id)
                db.session.add(replacement_interview)
                db.session.flush()
                replacement_interview_id = int(replacement_interview.id)
                replacement_media = MediaFile(
                    interview_id=replacement_interview_id,
                    original_filename="replacement.wav",
                    stored_path=f"{replacement_interview_id}/replacement.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(replacement_media)
                db.session.flush()
                replacement_media_id = int(replacement_media.id)
                replacement_transcription = Transcription(
                    media_file_id=replacement_media_id,
                    whisper_model="test",
                    language="ja",
                    status="done",
                    started_at=started,
                    completed_at=completed,
                )
                db.session.add(replacement_transcription)
                db.session.commit()
                replacement_transcription_id = int(replacement_transcription.id)

                failures += check(
                    "SQLite reused deleted project/interview/transcription integer IDs in fixture",
                    replacement_project_id == project_id
                    and replacement_interview_id == interview_id
                    and replacement_transcription_id == transcription_id,
                    (
                        f"project={project_id}->{replacement_project_id} "
                        f"interview={interview_id}->{replacement_interview_id} "
                        f"transcription={transcription_id}->{replacement_transcription_id}"
                    ),
                )

                con = sqlite3.connect(db_path)
                con.row_factory = sqlite3.Row
                try:
                    current_rows = con.execute(
                        """
                        SELECT tr.id, mf.interview_id, tr.started_at, tr.completed_at
                        FROM transcriptions tr
                        JOIN media_files mf ON mf.id=tr.media_file_id
                        WHERE tr.status='done'
                        ORDER BY tr.id
                        """
                    ).fetchall()
                    tombstoned_names = load_raw_snapshot_tombstone_names(con)
                finally:
                    con.close()
                by_transcription, invalid = load_raw_text_snapshots(Path(config.OUTPUT_DIR))
                missing = missing_raw_snapshot_transcription_ids(
                    current_rows,
                    by_transcription,
                    tombstoned_names,
                )
                failures += check(
                    "stable owner tombstones reject retained predecessors after identical ID reuse",
                    replacement_transcription_id in missing
                    and raw_snapshot.name in tombstoned_names
                    and legacy_snapshot.name in tombstoned_names
                    and not invalid,
                    f"missing={missing} tombstones={sorted(tombstoned_names)} invalid={invalid}",
                )

                replacement_snapshot = raw_dir / f"transcription_{transcription_id}_replacement.json"
                write_snapshot(
                    replacement_snapshot,
                    transcription_id=replacement_transcription_id,
                    interview_id=replacement_interview_id,
                    created_at_utc="2026-09-13T00:05:00+00:00",
                    text="replacement owner source",
                )
                by_transcription, invalid = load_raw_text_snapshots(Path(config.OUTPUT_DIR))
                missing = missing_raw_snapshot_transcription_ids(
                    current_rows,
                    by_transcription,
                    tombstoned_names,
                )
                failures += check(
                    "replacement owner must provide its own non-tombstoned snapshot",
                    replacement_transcription_id not in missing and not invalid,
                    f"missing={missing} invalid={invalid}",
                )

                rollback_project = Project(name="Rollback provenance")
                db.session.add(rollback_project)
                db.session.flush()
                rollback_project_id = int(rollback_project.id)
                rollback_interview = Interview(project_id=rollback_project_id)
                db.session.add(rollback_interview)
                db.session.flush()
                rollback_interview_id = int(rollback_interview.id)
                rollback_media = MediaFile(
                    interview_id=rollback_interview_id,
                    original_filename="rollback.wav",
                    stored_path=f"{rollback_interview_id}/rollback.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(rollback_media)
                db.session.flush()
                rollback_transcription = Transcription(
                    media_file_id=int(rollback_media.id),
                    whisper_model="test",
                    language="ja",
                    status="done",
                    started_at=datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc),
                    completed_at=datetime(2026, 9, 13, 2, 10, tzinfo=timezone.utc),
                )
                db.session.add(rollback_transcription)
                db.session.commit()
                rollback_transcription_id = int(rollback_transcription.id)
                rollback_snapshot = raw_dir / f"transcription_{rollback_transcription_id}_rollback.json"
                write_snapshot(
                    rollback_snapshot,
                    transcription_id=rollback_transcription_id,
                    interview_id=rollback_interview_id,
                    created_at_utc="2026-09-13T02:05:00+00:00",
                    text="rollback source",
                )

                session = db.session()

                def fail_before_commit(_session):
                    raise RuntimeError("simulated deletion commit failure")

                event.listen(session, "before_commit", fail_before_commit, once=True)
                rolled_back = False
                try:
                    delete_project(rollback_project)
                except RuntimeError:
                    rolled_back = True
                finally:
                    if event.contains(session, "before_commit", fail_before_commit):
                        event.remove(session, "before_commit", fail_before_commit)

                con = sqlite3.connect(db_path)
                try:
                    rollback_tombstone_count = int(
                        con.execute(
                            "SELECT COUNT(*) FROM raw_snapshot_tombstones WHERE snapshot_name=?",
                            (rollback_snapshot.name,),
                        ).fetchone()[0]
                    )
                finally:
                    con.close()
                failures += check(
                    "failed project deletion rolls back provenance tombstone with DB rows",
                    rolled_back
                    and db.session.get(Project, rollback_project_id) is not None
                    and rollback_tombstone_count == 0
                    and rollback_snapshot.is_file(),
                    f"tombstones={rollback_tombstone_count}",
                )

                db.session.remove()
                db.engine.dispose()

        finally:
            config.BASE_DIR = original["BASE_DIR"]
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
