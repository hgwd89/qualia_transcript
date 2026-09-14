import io
import sys
import tempfile
from pathlib import Path

from werkzeug.datastructures import FileStorage


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def issue_for(items: list[dict], code: str, media_file_id: int) -> dict | None:
    for item in items:
        if item.get("code") != code:
            continue
        context = item.get("context") or {}
        if int(context.get("media_file_id") or 0) == int(media_file_id):
            return item
    return None


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    for entry in (repo_root, scripts_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }

    with tempfile.TemporaryDirectory(prefix="qualia_media_readiness_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        output_dir = root / "outputs"
        upload_dir = root / "recovery_uploads"
        decoy_upload_dir = root / "configured_live_uploads"
        backup_dir = root / "backups"
        decoy_upload_dir.mkdir(parents=True)
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.UPLOAD_DIR = str(upload_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            import audit_production_readiness_project as project_readiness
            import audit_production_readiness_v2 as readiness
            from models import db
            from models.interview import Interview, MediaFile
            from models.project import Project
            from services.storage_paths import open_managed_file_for_create
            from services.upload_manager import (
                prepare_media_upload_target,
                save_and_register_media,
            )

            app = create_app()
            with app.app_context():
                project = Project(name="Media readiness integrity")
                db.session.add(project)
                db.session.flush()
                project_id = int(project.id)
                interview = Interview(project_id=project.id)
                db.session.add(interview)
                db.session.flush()

                original_bytes = b"RIFF-source-media-payload"
                media = save_and_register_media(
                    FileStorage(
                        stream=io.BytesIO(original_bytes),
                        filename="source.wav",
                        content_type="audio/wav",
                    ),
                    interview,
                    original_filename="source.wav",
                    mime_type="audio/wav",
                )
                media_id = int(media.id)
                media_path = upload_dir / str(media.stored_path)

                legacy_bytes = b"legacy-source-media"
                legacy_target = prepare_media_upload_target(interview.id, "legacy.wav")
                opened = open_managed_file_for_create(config.UPLOAD_DIR, legacy_target.stored_path)
                try:
                    opened.stream.write(legacy_bytes)
                finally:
                    opened.close()
                legacy = MediaFile(
                    interview_id=int(interview.id),
                    original_filename="legacy.wav",
                    stored_path=legacy_target.stored_path,
                    file_type="audio",
                    mime_type="audio/wav",
                    file_size_bytes=len(legacy_bytes),
                    content_sha256=None,
                )
                db.session.add(legacy)
                db.session.commit()
                legacy_id = int(legacy.id)
                db.session.remove()
                db.engine.dispose()

            # Simulate auditing an alternate recovery set while the process config
            # still points at a different live uploads tree.
            config.UPLOAD_DIR = str(decoy_upload_dir)

            default_root_report = readiness.audit(db_path, output_dir, backup_dir)
            failures += check(
                "default readiness still uses configured upload root",
                issue_for(
                    default_root_report.get("blockers", []),
                    "media_file_byte_integrity_invalid",
                    media_id,
                ) is not None,
                f"blockers={default_root_report.get('blockers', [])}",
            )

            report = readiness.audit(db_path, output_dir, backup_dir, upload_dir)
            counts = (report.get("info") or {}).get("media_file_byte_integrity") or {}
            failures += check(
                "explicit recovery-set upload root verifies hashed media and preserves legacy warning",
                counts == {"verified": 1, "unproven": 1, "invalid": 0}
                and (report.get("info") or {}).get("upload_dir") == str(upload_dir.resolve())
                and issue_for(
                    report.get("warnings", []),
                    "media_file_byte_integrity_unproven",
                    legacy_id,
                ) is not None
                and issue_for(
                    report.get("blockers", []),
                    "media_file_byte_integrity_invalid",
                    media_id,
                ) is None,
                f"counts={counts} info={report.get('info', {})} blockers={report.get('blockers', [])} warnings={report.get('warnings', [])}",
            )

            project_report = project_readiness.audit_project(
                db_path,
                output_dir,
                backup_dir,
                project_id,
                upload_dir,
            )
            project_counts = (project_report.get("info") or {}).get("media_file_byte_integrity") or {}
            failures += check(
                "project readiness propagates explicit recovery-set upload root",
                project_counts == {"verified": 1, "unproven": 1, "invalid": 0}
                and (project_report.get("info") or {}).get("upload_dir") == str(upload_dir.resolve())
                and issue_for(
                    project_report.get("blockers", []),
                    "media_file_byte_integrity_invalid",
                    media_id,
                ) is None,
                f"counts={project_counts} info={project_report.get('info', {})} blockers={project_report.get('blockers', [])}",
            )

            tampered = b"X" * len(original_bytes)
            media_path.write_bytes(tampered)
            tampered_report = readiness.audit(db_path, output_dir, backup_dir, upload_dir)
            failures += check(
                "in-place source media tampering blocks professional readiness",
                issue_for(
                    tampered_report.get("blockers", []),
                    "media_file_byte_integrity_invalid",
                    media_id,
                ) is not None,
                f"blockers={tampered_report.get('blockers', [])}",
            )

            media_path.write_bytes(original_bytes)
            restored_report = readiness.audit(db_path, output_dir, backup_dir, upload_dir)
            restored_counts = (restored_report.get("info") or {}).get("media_file_byte_integrity") or {}
            failures += check(
                "restoring exact uploaded media bytes restores verified readiness state",
                restored_counts.get("verified") == 1
                and issue_for(
                    restored_report.get("blockers", []),
                    "media_file_byte_integrity_invalid",
                    media_id,
                ) is None,
                f"counts={restored_counts} blockers={restored_report.get('blockers', [])}",
            )

            media_path.unlink()
            missing_report = readiness.audit(db_path, output_dir, backup_dir, upload_dir)
            failures += check(
                "missing registered source media blocks professional readiness",
                issue_for(
                    missing_report.get("blockers", []),
                    "media_file_byte_integrity_invalid",
                    media_id,
                ) is not None,
                f"blockers={missing_report.get('blockers', [])}",
            )
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            if original["BACKUP_DIR"] is not None:
                config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
