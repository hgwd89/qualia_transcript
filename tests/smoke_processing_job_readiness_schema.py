import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path


# The readiness implementation temporarily replaces the base audit's read-only
# connection factory when borrowing its pinned snapshot. This focused schema test
# stubs base_audit entirely, so expose the same global slot without opening a DB.
_connect_ro = None


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def load_audit(repo_root: Path):
    scripts_dir = repo_root / "scripts"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    path = scripts_dir / "audit_production_readiness_v2.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_v2_schema", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_fixture(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE processing_jobs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                interview_id INTEGER,
                question_id INTEGER,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                progress_json TEXT,
                created_at DATETIME,
                started_at DATETIME
            );
            CREATE TABLE interview_flow_questions (id INTEGER PRIMARY KEY);
            CREATE TABLE generated_files (
                id INTEGER PRIMARY KEY,
                file_format TEXT,
                stored_path TEXT
            );
            CREATE TABLE media_files (
                id INTEGER PRIMARY KEY,
                interview_id INTEGER
            );
            CREATE TABLE transcriptions (
                id INTEGER PRIMARY KEY,
                media_file_id INTEGER,
                status TEXT,
                started_at DATETIME,
                completed_at DATETIME
            );
            """
        )
        con.commit()
    finally:
        con.close()


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    audit_mod = load_audit(repo_root)

    # Keep this regression focused on the processing_jobs schema readiness gate.
    audit_mod.base_audit = lambda *_args, **_kwargs: {"blockers": [], "warnings": [], "info": {}}
    audit_mod.load_raw_text_snapshots = lambda _output_dir: ({}, [])
    audit_mod.load_raw_snapshot_tombstone_names = lambda _con: set()
    audit_mod.missing_raw_snapshot_transcription_ids = lambda *_args, **_kwargs: []
    audit_mod.validate_latest_backup = lambda _backup_dir: (None, None)

    failures = 0
    with tempfile.TemporaryDirectory(prefix="qualia_processing_job_schema_") as tmp:
        root = Path(tmp)
        db_path = root / "legacy.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        output_dir.mkdir()
        backup_dir.mkdir()
        create_fixture(db_path)

        legacy_report = audit_mod.audit(db_path, output_dir, backup_dir)
        legacy_codes = {item["code"] for item in legacy_report["blockers"]}
        failures += check(
            "readiness blocks legacy processing_jobs without request_json",
            "processing_job_request_json_column_missing" in legacy_codes,
            str(legacy_codes),
        )

        con = sqlite3.connect(db_path)
        try:
            con.execute("ALTER TABLE processing_jobs ADD COLUMN request_json TEXT")
            con.commit()
        finally:
            con.close()

        upgraded_report = audit_mod.audit(db_path, output_dir, backup_dir)
        upgraded_codes = {item["code"] for item in upgraded_report["blockers"]}
        failures += check(
            "request_json schema blocker clears after upgrade",
            "processing_job_request_json_column_missing" not in upgraded_codes,
            str(upgraded_codes),
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
