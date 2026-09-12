import json
import os
import sqlite3
import stat
import sys
import tempfile
import zipfile
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


def rewrite_database_member(source: Path, destination: Path, new_db_name: str) -> None:
    with zipfile.ZipFile(source, "r") as zf:
        payloads = {info.filename: zf.read(info.filename) for info in zf.infolist() if not info.is_dir()}
    manifest = json.loads(payloads["manifest.json"].decode("utf-8"))
    old_db_name = str(manifest["database_archive_path"])
    db_payload = payloads.pop(old_db_name)
    payloads[new_db_name] = db_payload
    manifest["database_archive_path"] = new_db_name
    for entry in manifest["files"]:
        if entry.get("path") == old_db_name:
            entry["path"] = new_db_name
            break
    payloads["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in payloads.items():
            zf.writestr(name, data)


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    import services.local_backup as local_backup

    failures += check(
        "default backup DB path matches Flask instance database",
        local_backup._sqlite_path() == Path(config.DATABASE_PATH).resolve()
        and Path(config.DATABASE_PATH).parent == Path(config.INSTANCE_DIR),
        str(local_backup._sqlite_path()),
    )
    failures += check(
        "legacy relative SQLite URI resolves under instance directory",
        local_backup._sqlite_path("sqlite:///legacy.db")
        == (Path(config.INSTANCE_DIR) / "legacy.db").resolve(),
    )

    with tempfile.TemporaryDirectory(prefix="qualia_backup_hardening_") as tmp:
        root = Path(tmp)
        source_db = root / "source.db"
        uploads = root / "uploads"
        outputs = root / "outputs"
        backups = root / "backups"
        uploads.mkdir()
        outputs.mkdir()
        (uploads / "audio.txt").write_text("audio", encoding="utf-8")
        (outputs / "report.txt").write_text("report", encoding="utf-8")
        create_db(source_db, "validated-source")
        source_uri = f"sqlite:///{source_db.as_posix()}"

        archive = local_backup.create_backup(
            backups,
            database_uri=source_uri,
            upload_dir=uploads,
            output_dir=outputs,
            label="hardening",
        )
        failures += check("backup archive validates after creation", local_backup.validate_backup(archive).get("format_version") == 1)
        if os.name != "nt":
            archive_mode = stat.S_IMODE(archive.stat().st_mode)
            dir_mode = stat.S_IMODE(backups.stat().st_mode)
            failures += check(
                "backup directory/archive are owner-only on POSIX",
                (dir_mode & 0o077) == 0 and (archive_mode & 0o077) == 0,
                f"dir={oct(dir_mode)} archive={oct(archive_mode)}",
            )

        custom_archive = root / "custom-db-path.zip"
        rewrite_database_member(archive, custom_archive, "database/custom.sqlite")
        custom_manifest = local_backup.validate_backup(custom_archive)
        failures += check(
            "noncanonical manifest database path validates when declared",
            custom_manifest.get("database_archive_path") == "database/custom.sqlite",
        )
        custom_target = root / "custom-target.db"
        custom_uploads = root / "custom-uploads"
        custom_outputs = root / "custom-outputs"
        local_backup.restore_backup(
            custom_archive,
            apply=True,
            database_uri=f"sqlite:///{custom_target.as_posix()}",
            upload_dir=custom_uploads,
            output_dir=custom_outputs,
            backup_dir=backups,
            create_pre_restore_backup=False,
        )
        failures += check(
            "restore uses manifest-declared database path",
            custom_target.is_file() and read_value(custom_target) == "validated-source",
        )

        immutable_target = root / "immutable-target.db"
        original_validate = local_backup.validate_backup
        mutated = {"done": False}

        def validate_then_replace_original(path):
            manifest = original_validate(path)
            if Path(path).name == "validated_source.zip" and not mutated["done"]:
                archive.write_bytes(b"source archive replaced after staged validation")
                mutated["done"] = True
            return manifest

        local_backup.validate_backup = validate_then_replace_original
        try:
            local_backup.restore_backup(
                archive,
                apply=True,
                database_uri=f"sqlite:///{immutable_target.as_posix()}",
                upload_dir=root / "immutable-uploads",
                output_dir=root / "immutable-outputs",
                backup_dir=backups,
                create_pre_restore_backup=False,
            )
        finally:
            local_backup.validate_backup = original_validate
        failures += check(
            "restore continues from staged validated bytes if source archive changes",
            mutated["done"]
            and immutable_target.is_file()
            and read_value(immutable_target) == "validated-source",
        )

        # Recreate a valid archive because the previous check intentionally replaced it.
        archive = local_backup.create_backup(
            backups,
            database_uri=source_uri,
            upload_dir=uploads,
            output_dir=outputs,
            label="rollback",
        )

        rollback_guard_target = root / "rollback-guard-current.db"
        rollback_guard_uploads = root / "rollback-guard-uploads"
        rollback_guard_outputs = root / "rollback-guard-outputs"
        create_db(rollback_guard_target, "rollback-current")
        rollback_guard_uploads.mkdir()
        rollback_guard_outputs.mkdir()
        guarded_upload = rollback_guard_uploads / "keep.txt"
        guarded_output = rollback_guard_outputs / "keep.txt"
        guarded_upload.write_text("keep upload", encoding="utf-8")
        guarded_output.write_text("keep output", encoding="utf-8")

        saved_link_check = local_backup.is_link_or_reparse
        local_backup.is_link_or_reparse = lambda path: Path(path) == guarded_upload
        linked_rollback_rejected = False
        try:
            local_backup.restore_backup(
                archive,
                apply=True,
                database_uri=f"sqlite:///{rollback_guard_target.as_posix()}",
                upload_dir=rollback_guard_uploads,
                output_dir=rollback_guard_outputs,
                backup_dir=backups,
                create_pre_restore_backup=False,
            )
        except ValueError as exc:
            linked_rollback_rejected = (
                "restore rollback" in str(exc) and "linked/reparse" in str(exc)
            )
        finally:
            local_backup.is_link_or_reparse = saved_link_check
        failures += check(
            "restore rejects linked rollback trees before mutating live targets",
            linked_rollback_rejected
            and read_value(rollback_guard_target) == "rollback-current"
            and guarded_upload.read_text(encoding="utf-8") == "keep upload"
            and guarded_output.read_text(encoding="utf-8") == "keep output",
        )

        new_target = root / "new-target.db"
        saved_copy_tree = local_backup._copy_tree_from_stage

        def fail_upload_copy(stage, prefix, destination):
            if prefix == "uploads":
                raise RuntimeError("simulated post-database restore failure")
            return saved_copy_tree(stage, prefix, destination)

        local_backup._copy_tree_from_stage = fail_upload_copy
        rollback_failed = False
        try:
            local_backup.restore_backup(
                archive,
                apply=True,
                database_uri=f"sqlite:///{new_target.as_posix()}",
                upload_dir=root / "new-uploads",
                output_dir=root / "new-outputs",
                backup_dir=backups,
                create_pre_restore_backup=False,
            )
        except RuntimeError:
            rollback_failed = True
        finally:
            local_backup._copy_tree_from_stage = saved_copy_tree
        failures += check(
            "failed restore removes a database that did not exist before",
            rollback_failed and not new_target.exists(),
        )

        corrupt_target = root / "corrupt-current.db"
        corrupt_bytes = b"not a sqlite database but must be preserved"
        corrupt_target.write_bytes(corrupt_bytes)
        recovery = local_backup.restore_backup(
            archive,
            apply=True,
            database_uri=f"sqlite:///{corrupt_target.as_posix()}",
            upload_dir=root / "corrupt-uploads",
            output_dir=root / "corrupt-outputs",
            backup_dir=backups,
            allow_unvalidated_pre_restore=True,
        )
        preserved = Path(recovery.get("pre_restore_database_copy") or "")
        failures += check(
            "explicit damaged-DB recovery preserves raw pre-restore database copy",
            preserved.is_file()
            and preserved.read_bytes() == corrupt_bytes
            and read_value(corrupt_target) == "validated-source",
            str(preserved),
        )

    backup_cli = (repo_root / "scripts" / "backup_local_data.py").read_text(encoding="utf-8")
    failures += check(
        "backup CLI does not perform a second full validate pass",
        "validate_backup" not in backup_cli,
    )
    restore_cli = (repo_root / "scripts" / "restore_local_data.py").read_text(encoding="utf-8")
    failures += check(
        "restore CLI exposes explicit damaged-DB recovery acknowledgement",
        "--allow-unvalidated-pre-restore" in restore_cli,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
