import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path


# audit_production_readiness_v2 temporarily swaps the base audit connection factory.
_connect_ro = None


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def load_audit(repo_root: Path):
    scripts_dir = repo_root / "scripts"
    for entry in (repo_root, scripts_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    path = scripts_dir / "audit_production_readiness_v2.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_v2_backup_freshness", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_fixture(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE interview_flow_questions (id INTEGER PRIMARY KEY);
            CREATE TABLE processing_jobs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                interview_id INTEGER,
                question_id INTEGER REFERENCES interview_flow_questions(id),
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                progress_json TEXT,
                request_json TEXT,
                created_at DATETIME,
                started_at DATETIME
            );
            CREATE TABLE media_files (
                id INTEGER PRIMARY KEY,
                interview_id INTEGER,
                stored_path TEXT,
                file_size_bytes INTEGER,
                content_sha256 TEXT
            );
            CREATE TABLE transcriptions (
                id INTEGER PRIMARY KEY,
                media_file_id INTEGER,
                status TEXT,
                started_at DATETIME,
                completed_at DATETIME
            );
            CREATE TABLE generated_files (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                file_type TEXT,
                file_format TEXT,
                stored_path TEXT,
                generation_params_json TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


def pinned_connection(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN")
    con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    return con


def blocker_codes(report: dict) -> set[str]:
    return {str(item.get("code")) for item in report.get("blockers", [])}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from services.local_backup import DB_ARCHIVE_PATH, create_backup
    from services.readiness_backup_freshness import compare_latest_backup_to_current_recovery_set

    audit_mod = load_audit(repo_root)
    audit_mod.base_audit = lambda *_args, **_kwargs: {"blockers": [], "warnings": [], "info": {}}

    failures = 0
    with tempfile.TemporaryDirectory(prefix="qualia_backup_readiness_freshness_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        upload_dir = root / "uploads"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        upload_dir.mkdir()
        output_dir.mkdir()
        backup_dir.mkdir()
        create_fixture(db_path)

        upload_path = upload_dir / "source.bin"
        output_path = output_dir / "report.txt"
        upload_path.write_bytes(b"source-v1")
        output_path.write_text("report-v1", encoding="utf-8")

        create_backup(
            backup_dir,
            database_uri=f"sqlite:///{db_path.as_posix()}",
            upload_dir=upload_dir,
            output_dir=output_dir,
            label="freshness_initial",
        )

        con = pinned_connection(db_path)
        try:
            fresh = compare_latest_backup_to_current_recovery_set(
                con, backup_dir, upload_dir, output_dir
            )
        finally:
            con.rollback()
            con.close()
        failures += check(
            "fresh backup exactly matches DB/uploads/outputs recovery set",
            fresh.matches_current is True
            and fresh.validation_error is None
            and fresh.comparison_error is None
            and fresh.backup_file_count == fresh.current_file_count == 3,
            str(fresh),
        )

        report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "production readiness accepts a byte-current latest backup",
            "latest_backup_stale" not in blocker_codes(report)
            and report.get("info", {}).get("latest_backup_matches_current_recovery_set") is True,
            f"blockers={report.get('blockers', [])} info={report.get('info', {})}",
        )

        # Prove the DB comparison uses the caller's pinned SQLite snapshot rather
        # than reopening the latest database state midway through readiness.
        pinned = pinned_connection(db_path)
        writer = sqlite3.connect(db_path)
        try:
            writer.execute("INSERT INTO interview_flow_questions(id) VALUES (1)")
            writer.commit()
        finally:
            writer.close()
        try:
            pinned_result = compare_latest_backup_to_current_recovery_set(
                pinned, backup_dir, upload_dir, output_dir
            )
        finally:
            pinned.rollback()
            pinned.close()
        failures += check(
            "backup comparison stays bound to the already-established SQLite read snapshot",
            pinned_result.matches_current is True,
            str(pinned_result),
        )

        current = pinned_connection(db_path)
        try:
            db_stale = compare_latest_backup_to_current_recovery_set(
                current, backup_dir, upload_dir, output_dir
            )
        finally:
            current.rollback()
            current.close()
        failures += check(
            "post-backup database change makes the newest backup stale",
            db_stale.matches_current is False
            and any(item.get("path") == DB_ARCHIVE_PATH for item in db_stale.changed_files),
            str(db_stale),
        )

        stale_report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "production readiness blocks a valid but stale database backup",
            "latest_backup_stale" in blocker_codes(stale_report)
            and stale_report.get("info", {}).get("latest_backup_matches_current_recovery_set") is False,
            str(stale_report.get("blockers", [])),
        )

        create_backup(
            backup_dir,
            database_uri=f"sqlite:///{db_path.as_posix()}",
            upload_dir=upload_dir,
            output_dir=output_dir,
            label="freshness_after_db",
        )
        refreshed_report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "new backup after DB change restores recovery freshness",
            "latest_backup_stale" not in blocker_codes(refreshed_report)
            and refreshed_report.get("info", {}).get("latest_backup_matches_current_recovery_set") is True,
            str(refreshed_report.get("blockers", [])),
        )

        upload_path.write_bytes(b"source-v2")
        con = pinned_connection(db_path)
        try:
            upload_stale = compare_latest_backup_to_current_recovery_set(
                con, backup_dir, upload_dir, output_dir
            )
        finally:
            con.rollback()
            con.close()
        failures += check(
            "same-size upload byte change makes backup stale by SHA-256",
            upload_stale.matches_current is False
            and any(item.get("path") == "uploads/source.bin" for item in upload_stale.changed_files),
            str(upload_stale.changed_files),
        )

        upload_path.write_bytes(b"source-v1")
        added_output = output_dir / "new.txt"
        added_output.write_text("new", encoding="utf-8")
        con = pinned_connection(db_path)
        try:
            added = compare_latest_backup_to_current_recovery_set(
                con, backup_dir, upload_dir, output_dir
            )
        finally:
            con.rollback()
            con.close()
        failures += check(
            "current-only output file is reported missing from backup",
            added.matches_current is False
            and any(item.get("path") == "outputs/new.txt" for item in added.missing_from_backup),
            str(added.missing_from_backup),
        )
        added_output.unlink()

        output_path.unlink()
        con = pinned_connection(db_path)
        try:
            deleted = compare_latest_backup_to_current_recovery_set(
                con, backup_dir, upload_dir, output_dir
            )
        finally:
            con.rollback()
            con.close()
        failures += check(
            "backup-only output file is reported as stale extra recovery content",
            deleted.matches_current is False
            and any(item.get("path") == "outputs/report.txt" for item in deleted.extra_in_backup),
            str(deleted.extra_in_backup),
        )
        output_path.write_text("report-v1", encoding="utf-8")

        final_report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "exact file restoration returns newest backup to current state",
            "latest_backup_stale" not in blocker_codes(final_report)
            and final_report.get("info", {}).get("latest_backup_matches_current_recovery_set") is True,
            str(final_report.get("blockers", [])),
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
