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
        live_upload_project = live_uploads / "project-1"
        live_output_project = live_outputs / "project-1"
        live_upload_project.mkdir()
        live_output_project.mkdir()
        (live_uploads / "current.txt").write_text("current upload", encoding="utf-8")
        (live_outputs / "current.txt").write_text("current output", encoding="utf-8")
        (live_upload_project / "partial.txt").write_text("partial upload", encoding="utf-8")
        (live_output_project / "partial.txt").write_text("partial output", encoding="utf-8")
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

        partial_restore_db = root / "partial-restore" / "target.db"
        partial_restore_output = root / "partial-restore" / "outputs"
        partial_restore_upload = root / "partial-restore" / "uploads"
        partial_restore_output.mkdir(parents=True)
        partial_restore_upload.mkdir(parents=True)
        partial_restore_uri = f"sqlite:///{partial_restore_db.as_posix()}"

        try:
            failures += check(
                "fully disjoint explicit fixture paths remain outside the live recovery set",
                not local_backup._touches_live_recovery_set(
                    fixture_uri,
                    fixture_uploads,
                    fixture_outputs,
                ),
            )
            failures += check(
                "live upload descendants are classified as live recovery targets",
                local_backup._touches_live_recovery_set(
                    fixture_uri,
                    live_upload_project,
                    fixture_outputs,
                ),
            )
            failures += check(
                "ancestors of a live managed root are classified as live recovery targets",
                local_backup._touches_live_recovery_set(
                    fixture_uri,
                    fixture_uploads,
                    live_root,
                ),
            )
            failures += check(
                "cross-role references to another live managed root are classified as live",
                local_backup._touches_live_recovery_set(
                    fixture_uri,
                    live_outputs,
                    fixture_outputs,
                ),
            )

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

                partial_backup_refused = False
                try:
                    local_backup.create_backup(
                        fixture_backups,
                        database_uri=fixture_uri,
                        upload_dir=live_upload_project,
                        output_dir=fixture_outputs,
                        label="partial-live-upload",
                    )
                except RuntimeLockError:
                    partial_backup_refused = True
                failures += check(
                    "backup of a live managed subtree cannot bypass maintenance exclusion",
                    partial_backup_refused,
                )

                ancestor_backup_refused = False
                try:
                    local_backup.create_backup(
                        fixture_backups,
                        database_uri=fixture_uri,
                        upload_dir=fixture_uploads,
                        output_dir=live_root,
                        label="ancestor-of-live-root",
                    )
                except RuntimeLockError:
                    ancestor_backup_refused = True
                failures += check(
                    "backup of an ancestor containing a live managed root cannot bypass maintenance exclusion",
                    ancestor_backup_refused,
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

                partial_restore_refused = False
                try:
                    local_backup.restore_backup(
                        archive,
                        apply=True,
                        database_uri=partial_restore_uri,
                        upload_dir=live_upload_project,
                        output_dir=partial_restore_output,
                        backup_dir=fixture_backups,
                        create_pre_restore_backup=False,
                    )
                except RuntimeLockError:
                    partial_restore_refused = True
                failures += check(
                    "applied restore targeting a live subtree cannot bypass maintenance exclusion",
                    partial_restore_refused,
                )

                cross_role_restore_refused = False
                try:
                    local_backup.restore_backup(
                        archive,
                        apply=True,
                        database_uri=partial_restore_uri,
                        upload_dir=live_outputs,
                        output_dir=partial_restore_output,
                        backup_dir=fixture_backups,
                        create_pre_restore_backup=False,
                    )
                except RuntimeLockError:
                    cross_role_restore_refused = True
                failures += check(
                    "applied restore cannot bypass maintenance by passing a live path in another role",
                    cross_role_restore_refused,
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

    source_text = (repo_root / "services" / "local_backup.py").read_text(encoding="utf-8")
    failures += check(
        "service boundary compares recovery paths by ancestry rather than exact equality only",
        "def _paths_overlap" in source_text
        and "left in right.parents" in source_text
        and "right in left.parents" in source_text,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
