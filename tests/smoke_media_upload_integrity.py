import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def upload_files(upload_root: Path) -> set[str]:
    if not upload_root.exists():
        return set()
    return {
        str(path.relative_to(upload_root)).replace("\\", "/")
        for path in upload_root.rglob("*")
        if path.is_file()
    }


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_media_upload_") as tmp:
        root = Path(tmp)
        upload_root = root / "uploads"
        config.DATABASE_URI = f"sqlite:///{(root / 'media.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(upload_root)

        try:
            from sqlalchemy import event, inspect as sa_inspect
            from werkzeug.datastructures import FileStorage

            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile, Transcription
            from models.project import Project
            from models.segment import Segment
            import services.transcription as transcription_service
            import services.upload_manager as upload_manager_service
            from services.readiness_validation import (
                load_raw_text_snapshots,
                missing_raw_snapshot_transcription_ids,
            )
            from services.upload_manager import (
                create_media_read_snapshot,
                get_media_full_path,
                media_extension,
                media_file_exists,
                save_and_register_media,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                media_columns = {column["name"] for column in sa_inspect(db.engine).get_columns("media_files")}
                failures += check(
                    "media schema carries nullable content identity",
                    "content_sha256" in media_columns,
                    str(sorted(media_columns)),
                )

                project = Project(name="Media integrity")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                interview = Interview(project_id=project_id, notes="success")
                db.session.add(interview)
                db.session.flush()
                original_bytes = b"fake-media-bytes"
                storage = FileStorage(
                    stream=io.BytesIO(original_bytes),
                    filename="sample.mp3",
                    content_type="audio/mpeg",
                )
                media = save_and_register_media(
                    storage,
                    interview,
                    original_filename=storage.filename,
                    mime_type=storage.content_type,
                )
                media_path = Path(get_media_full_path(media))
                expected_sha256 = hashlib.sha256(original_bytes).hexdigest()
                failures += check(
                    "successful media upload binds DB metadata to exact written bytes",
                    media.id is not None
                    and media.interview_id == interview.id
                    and media_file_exists(media)
                    and media_path.read_bytes() == original_bytes
                    and media.file_size_bytes == len(original_bytes)
                    and media.content_sha256 == expected_sha256
                    and "\\" not in media.stored_path,
                    f"media_id={media.id} stored_path={media.stored_path} sha={media.content_sha256}",
                )

                snapshot = create_media_read_snapshot(media)
                snapshot_path = Path(snapshot.full_path)
                snapshot_outside_uploads = upload_root.resolve() not in snapshot_path.resolve().parents
                snapshot_initial = snapshot_path.read_bytes()
                id_dir = media_path.parent
                saved_id_dir = id_dir.with_name(f"{id_dir.name}.snapshot-original")
                snapshot_survived_replacement = False
                snapshot_removed = False
                try:
                    id_dir.rename(saved_id_dir)
                    id_dir.mkdir()
                    (id_dir / media_path.name).write_bytes(b"replacement-media")
                    snapshot_survived_replacement = snapshot_path.read_bytes() == original_bytes
                finally:
                    snapshot.close()
                    snapshot_removed = not snapshot_path.exists()
                    replacement = id_dir / media_path.name
                    if replacement.exists():
                        replacement.unlink()
                    if id_dir.exists():
                        id_dir.rmdir()
                    if saved_id_dir.exists():
                        saved_id_dir.rename(id_dir)
                failures += check(
                    "in-flight transcription snapshot remains stable after managed pathname replacement",
                    snapshot_initial == original_bytes
                    and snapshot_outside_uploads
                    and snapshot_survived_replacement
                    and snapshot_removed,
                    (
                        f"outside={snapshot_outside_uploads} "
                        f"stable={snapshot_survived_replacement} removed={snapshot_removed}"
                    ),
                )

                tampered_bytes = b"tampered-media!!"
                failures += check(
                    "tamper fixture preserves size so SHA-256 is the deciding identity check",
                    len(tampered_bytes) == len(original_bytes),
                    f"original={len(original_bytes)} tampered={len(tampered_bytes)}",
                )
                media_path.unlink()
                media_path.write_bytes(tampered_bytes)
                tamper_rejected = False
                try:
                    create_media_read_snapshot(media)
                except ValueError as exc:
                    tamper_rejected = "SHA-256" in str(exc)
                finally:
                    media_path.unlink(missing_ok=True)
                    media_path.write_bytes(original_bytes)
                failures += check(
                    "same-size regular-file replacement is rejected before transcription",
                    tamper_rejected,
                )

                persisted_digest = media.content_sha256
                media.content_sha256 = None
                db.session.commit()
                legacy_snapshot = create_media_read_snapshot(media)
                try:
                    legacy_ok = Path(legacy_snapshot.full_path).read_bytes() == original_bytes
                finally:
                    legacy_snapshot.close()
                media.content_sha256 = persisted_digest
                db.session.commit()
                failures += check(
                    "legacy media rows without a stored digest remain readable but are not backfilled",
                    legacy_ok and media.content_sha256 == expected_sha256,
                )

                tracked_open: dict[str, object] = {}
                real_open_managed_file = upload_manager_service.open_managed_file_for_read

                def tracking_open(root_value, stored_path):
                    opened = real_open_managed_file(root_value, stored_path)
                    tracked_open["value"] = opened
                    return opened

                tempdir_failure_raised = False
                with patch.object(
                    upload_manager_service,
                    "open_managed_file_for_read",
                    side_effect=tracking_open,
                ), patch.object(
                    upload_manager_service.tempfile,
                    "TemporaryDirectory",
                    side_effect=RuntimeError("simulated media snapshot tempdir failure"),
                ):
                    try:
                        create_media_read_snapshot(media)
                    except RuntimeError as exc:
                        tempdir_failure_raised = "simulated media snapshot tempdir failure" in str(exc)

                opened_after_failure = tracked_open.get("value")
                source_stream_closed = bool(
                    opened_after_failure is not None
                    and getattr(opened_after_failure.stream, "closed", False)
                )
                failures += check(
                    "snapshot temp-directory failure closes pinned source handle",
                    tempdir_failure_raised and source_stream_closed,
                    f"raised={tempdir_failure_raised} source_closed={source_stream_closed}",
                )

                failures += check(
                    "Japanese media filename preserves allowed extension",
                    media_extension("インタビュー音声.MP3") == ".mp3",
                )

                unsupported_rejected = False
                try:
                    media_extension("payload.exe")
                except ValueError:
                    unsupported_rejected = True
                failures += check(
                    "unsupported upload extension is rejected",
                    unsupported_rejected,
                )

                outside = root / "outside.mp3"
                outside.write_bytes(b"must-survive")
                malicious = MediaFile(
                    interview_id=int(interview.id),
                    original_filename="outside.mp3",
                    stored_path="../outside.mp3",
                    file_type="audio",
                    mime_type="audio/mpeg",
                )
                db.session.add(malicious)
                db.session.commit()
                rejected_escape = False
                try:
                    get_media_full_path(malicious)
                except ValueError:
                    rejected_escape = True
                failures += check(
                    "malicious media stored_path cannot escape UPLOAD_DIR",
                    rejected_escape and not media_file_exists(malicious) and outside.is_file(),
                )

                before_interviews = Interview.query.count()
                before_media = MediaFile.query.count()
                before_files = upload_files(upload_root)

                pending = Interview(project_id=project_id, notes="commit-failure")
                db.session.add(pending)
                db.session.flush()
                failed_interview_id = int(pending.id)
                failed_storage = FileStorage(
                    stream=io.BytesIO(b"orphan-candidate"),
                    filename="failure.wav",
                    content_type="audio/wav",
                )

                session = db.session()

                def fail_before_commit(_session):
                    raise RuntimeError("simulated media DB commit failure")

                event.listen(session, "before_commit", fail_before_commit, once=True)
                raised = False
                try:
                    save_and_register_media(
                        failed_storage,
                        pending,
                        original_filename=failed_storage.filename,
                        mime_type=failed_storage.content_type,
                    )
                except RuntimeError as exc:
                    raised = "simulated media DB commit failure" in str(exc)
                finally:
                    if event.contains(session, "before_commit", fail_before_commit):
                        event.remove(session, "before_commit", fail_before_commit)

                after_files = upload_files(upload_root)
                failures += check(
                    "media DB commit failure removes uploaded orphan and rolls back rows",
                    raised
                    and Interview.query.count() == before_interviews
                    and MediaFile.query.count() == before_media
                    and db.session.get(Interview, failed_interview_id) is None
                    and after_files == before_files,
                    (
                        f"raised={raised} interviews={Interview.query.count()}/{before_interviews} "
                        f"media={MediaFile.query.count()}/{before_media} files={after_files}"
                    ),
                )

                class FakeLocalSegment:
                    def __init__(self, start, end, text, speaker=None):
                        self.start = start
                        self.end = end
                        self.text = text
                        self.speaker = speaker

                class FakeLocalModel:
                    def transcribe(self, *_args, **_kwargs):
                        return iter([
                            FakeLocalSegment(0.0, 1.0, " えーっと", "A"),
                            FakeLocalSegment(1.0, 2.0, " そのままです。 ", "A"),
                        ]), object()

                local_tr = Transcription(
                    media_file_id=int(media.id),
                    whisper_model="tiny",
                    language="ja",
                    status="pending",
                )
                db.session.add(local_tr)
                db.session.commit()
                local_tr_id = int(local_tr.id)
                expected_raw_text = " えーっと そのままです。 "
                expected_raw_sha = hashlib.sha256(expected_raw_text.encode("utf-8")).hexdigest()

                with patch.object(
                    transcription_service,
                    "_get_model",
                    return_value=FakeLocalModel(),
                ):
                    local_result = transcription_service.run_local_whisper_transcription(local_tr_id)

                local_tr = db.session.get(Transcription, local_tr_id)
                local_snapshot_path = Path(config.OUTPUT_DIR) / local_result["raw_snapshot_path"]
                local_payload = json.loads(local_snapshot_path.read_text(encoding="utf-8"))
                local_segment_texts = [
                    row.text
                    for row in Segment.query.filter_by(transcription_id=local_tr_id)
                    .order_by(Segment.seq)
                    .all()
                ]
                by_transcription, invalid_snapshots = load_raw_text_snapshots(Path(config.OUTPUT_DIR))
                missing_local = missing_raw_snapshot_transcription_ids(
                    [{
                        "id": local_tr_id,
                        "interview_id": int(interview.id),
                        "started_at": local_tr.started_at,
                        "completed_at": local_tr.completed_at,
                    }],
                    by_transcription,
                )
                failures += check(
                    "local Whisper success persists exact provider text as current-generation raw evidence",
                    local_tr.status == "done"
                    and local_snapshot_path.is_file()
                    and local_payload.get("text") == expected_raw_text
                    and local_payload.get("sha256") == expected_raw_sha
                    and local_payload.get("snapshot_tag") == "local_whisper"
                    and local_result.get("raw_text_sha256") == expected_raw_sha
                    and local_result.get("raw_snapshot_files") == [local_result.get("raw_snapshot_path")]
                    and local_segment_texts == ["えーっと", "そのままです。"]
                    and not invalid_snapshots
                    and missing_local == [],
                    (
                        f"status={local_tr.status} text={local_payload.get('text')!r} "
                        f"segments={local_segment_texts!r} invalid={invalid_snapshots!r} "
                        f"missing={missing_local!r}"
                    ),
                )

                db.session.remove()
                db.engine.dispose()

            interviews_source = (repo_root / "routes" / "interviews.py").read_text(encoding="utf-8")
            transcription_source = (repo_root / "services" / "transcription.py").read_text(encoding="utf-8")
            app_source = (repo_root / "app.py").read_text(encoding="utf-8")
            failures += check(
                "interview upload route uses managed media registration",
                "save_and_register_media(" in interviews_source,
            )
            failures += check(
                "transcription uses verified managed-media snapshots instead of reopening upload paths",
                transcription_source.count("create_media_read_snapshot(media)") == 2
                and "get_media_full_path(media)" not in transcription_source
                and transcription_source.count("media_snapshot.close()") == 2
                and "os.path.join(config.UPLOAD_DIR, media.stored_path)" not in transcription_source,
            )
            failures += check(
                "local Whisper cannot mark success without immutable raw provider evidence",
                'raw_text = "".join(str(getattr(seg, "text", "") or "") for seg in local_segments)' in transcription_source
                and 'raise RuntimeError("local Whisper transcription returned empty text")' in transcription_source
                and 'snapshot_tag="local_whisper"' in transcription_source,
            )
            failures += check(
                "legacy databases receive the media content identity column additively",
                "ALTER TABLE media_files ADD COLUMN content_sha256 TEXT" in app_source,
            )

        finally:
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
