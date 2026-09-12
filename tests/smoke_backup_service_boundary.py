import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def create_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE source_data (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO source_data VALUES (1, ?)", (value,))
        con.commit()
    finally:
        con.close()


def read_value(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT value FROM source_data WHERE id=1").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    import services.local_backup as local_backup
    from services.runtime_lock import RuntimeLockError, runtime_lock

    with tempfile.TemporaryDirectory(prefix="qualia_backup_service_boundary_") as tmp:
        root = Path(tmp).resolve()

        fixture_db = root / "fixture" / "source.db"
        fixture_uploads = root / "fixture" / "uploads"
        fixture_outputs = root / "fixture" / "outputs"
        fixture_backups = root / "fixture" / "backups"
        fixture_uploads.mkdir(parents=True)
        fixture_outputs.mkdir(parents=True)
        (fixture_uploads / "audio.txt").write_text("audio", encoding="utf-8")
        (fixture_outputs / "report.txt").write_text("report", encoding="utf-8")
        create_db(fixture_db, "archive-source")
        fixture_uri = f"sqlite:///{fixture_db.as_posix()}"

        archive = local_backup.create_backup(
            fixture_backups,
            database_uri=fixture_uri,
            upload_dir=fixture_uploads,
            output_dir=fixture_outputs,
            label="service-boundary-fixture",
        )
        failures += check(
            "custom fixture backup remains available without live maintenance lock",
            archive.is_file(),
            str(archive),
        )

        saved_link_check = local_backup.is_link_or_reparse
        local_backup.is_link_or_reparse = lambda path: Path(path) == fixture_uploads / "audio.txt"
        linked_rejected = False
        try:
            local_backup.create_backup(
                fixture_backups,
                database_uri=fixture_uri,
                upload_dir=fixture_uploads,
                output_dir=fixture_outputs,
                label="linked-entry",
            )
        except ValueError as exc:
            linked_rejected = "linked/reparse" in str(exc)
        finally:
            local_backup.is_link_or_reparse = saved_link_check
        failures += check(
            "backup rejects linked or reparse entries below managed trees",
            linked_rejected,
        )

        live_root = root / "live"
        live_db = live_root / "instance" / "qualia.db"
        live_uploads = live_root / "uploads"
        live_outputs = live_root / "outputs"
        live_backups = live_root / "backups"
        live_lock = live_root / "instance" / "runtime.lock"
        live_uploads.mkdir(parents=True)
        live_outputs.mkdir(parents=True)
        (live_uploads / "current.txt").write_text("current upload", encoding="utf-8")
        (live_outputs / "current.txt").write_text("current output", encoding="utf-8")
        create_db(live_db, "live-current")

        original_config = {
            "DATABASE_PATH": config.DATABASE_PATH,
            "DATABASE_URI": config.DATABASE_URI,
            "UPLOAD_DIR": config.UPLOAD_DIR,
            "OUTPUT_DIR": config.OUTPUT_DIR,
            "BACKUP_DIR": config.BACKUP_DIR,
            "RUNTIME_LOCK_PATH": config.RUNTIME_LOCK_PATH,
        }
        config.DATABASE_PATH = str(live_db)
        config.DATABASE_URI = f"sqlite:///{live_db.as_posix()}"
        config.UPLOAD_DIR = str(live_uploads)
        config.OUTPUT_DIR = str(live_outputs)
        config.BACKUP_DIR = str(live_backups)
        config.RUNTIME_LOCK_PATH = str(live_lock)

        try:
            with runtime_lock("app", live_lock):
                backup_refused = False
                try:
                    local_backup.create_backup(label="must-refuse")
                except RuntimeLockError:
                    backup_refused = True
                failures += check(
                    "direct backup service refuses live runtime overlap",
                    backup_refused,
                )

                restore_refused = False
                try:
                    local_backup.restore_backup(
                        archive,
                        apply=True,
                        create_pre_restore_backup=False,
                    )
                except RuntimeLockError:
                    restore_refused = True
                failures += check(
                    "direct applied-restore service refuses live runtime overlap",
                    restore_refused,
                )
                failures += check(
                    "refused service restore leaves live database unchanged",
                    read_value(live_db) == "live-current",
                )
        finally:
            config.DATABASE_PATH = original_config["DATABASE_PATH"]
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]
            config.BACKUP_DIR = original_config["BACKUP_DIR"]
            config.RUNTIME_LOCK_PATH = original_config["RUNTIME_LOCK_PATH"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
