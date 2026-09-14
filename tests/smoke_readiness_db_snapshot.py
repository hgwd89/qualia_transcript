import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


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

    with tempfile.TemporaryDirectory(prefix="qualia_readiness_snapshot_") as tmp:
        root = Path(tmp)
        db_path = root / "snapshot.db"
        output_dir = root / "outputs"
        upload_dir = root / "uploads"
        backup_dir = root / "backups"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.UPLOAD_DIR = str(upload_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            from models import db
            from models.project import Project

            app = create_app()
            with app.app_context():
                project = Project(name="Readiness snapshot smoke")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)
                db.session.remove()
                db.engine.dispose()

            setup = sqlite3.connect(db_path)
            try:
                mode = setup.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            finally:
                setup.close()
            failures += check(
                "fixture uses WAL so a writer can commit while readiness holds a read snapshot",
                str(mode).lower() == "wal",
                f"journal_mode={mode}",
            )

            import audit_production_readiness_v2 as audit_mod

            base_globals = audit_mod.base_audit.__globals__
            original_tables = base_globals["_tables"]
            mutation = {"committed": False}

            def tables_then_mutate(connection):
                tables = original_tables(connection)
                if mutation["committed"]:
                    return tables
                writer = sqlite3.connect(db_path, timeout=5.0)
                try:
                    writer.execute(
                        """
                        INSERT INTO processing_jobs (
                            project_id, job_type, status, attempt_count, created_at
                        )
                        VALUES (?, 'project_pipeline', 'pending', 0, CURRENT_TIMESTAMP)
                        """,
                        (project_id,),
                    )
                    writer.commit()
                    mutation["committed"] = True
                finally:
                    writer.close()
                return tables

            base_globals["_tables"] = tables_then_mutate
            try:
                report = audit_mod.audit(db_path, output_dir, backup_dir)
            finally:
                base_globals["_tables"] = original_tables

            failures += check(
                "concurrent writer commits after readiness snapshot is established",
                mutation["committed"],
            )
            failures += check(
                "base and v2 readiness checks retain the same pre-write SQLite snapshot",
                report.get("info", {}).get("active_processing_job_count") == 0
                and report.get("info", {}).get("database_snapshot") == "single_read_transaction"
                and not any(
                    item.get("code") == "active_processing_jobs"
                    for item in report.get("warnings", [])
                ),
                f"info={report.get('info', {})} warnings={report.get('warnings', [])}",
            )

            blocker_codes = {item.get("code") for item in report.get("blockers", [])}
            start_version = report.get("info", {}).get("database_data_version_start")
            end_version = report.get("info", {}).get("database_data_version_end")
            failures += check(
                "readiness fails closed when the live database changes during the pinned audit",
                "database_changed_during_audit" in blocker_codes
                and report.get("info", {}).get("database_changed_during_audit") is True
                and isinstance(start_version, int)
                and isinstance(end_version, int)
                and start_version != end_version,
                f"blockers={report.get('blockers', [])} start={start_version} end={end_version}",
            )

            verify = sqlite3.connect(db_path)
            try:
                durable_count = int(
                    verify.execute(
                        "SELECT COUNT(*) FROM processing_jobs WHERE status='pending'"
                    ).fetchone()[0]
                )
                verify.execute(
                    "UPDATE processing_jobs SET status='completed' WHERE status='pending'"
                )
                verify.commit()
            finally:
                verify.close()
            failures += check(
                "writer commit is durable outside the pinned readiness snapshot",
                durable_count == 1,
                f"pending_jobs={durable_count}",
            )

            stable_report = audit_mod.audit(db_path, output_dir, backup_dir)
            stable_codes = {item.get("code") for item in stable_report.get("blockers", [])}
            stable_start = stable_report.get("info", {}).get("database_data_version_start")
            stable_end = stable_report.get("info", {}).get("database_data_version_end")
            failures += check(
                "quiescent readiness does not raise a database-change blocker",
                "database_changed_during_audit" not in stable_codes
                and "database_change_detection_failed" not in stable_codes
                and stable_report.get("info", {}).get("database_changed_during_audit") is False
                and stable_start == stable_end,
                f"blockers={stable_report.get('blockers', [])} start={stable_start} end={stable_end}",
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
