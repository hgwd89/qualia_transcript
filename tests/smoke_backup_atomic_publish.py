import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def create_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO sample VALUES (1, 'atomic-publish')")
        con.commit()
    finally:
        con.close()


def build_fixture(root: Path) -> tuple[Path, Path, Path, str]:
    database = root / "source.db"
    uploads = root / "uploads"
    outputs = root / "outputs"
    uploads.mkdir(parents=True)
    outputs.mkdir(parents=True)
    (uploads / "source.txt").write_text("source payload", encoding="utf-8")
    (outputs / "report.txt").write_text("report payload", encoding="utf-8")
    create_db(database)
    return database, uploads, outputs, f"sqlite:///{database.as_posix()}"


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import services.local_backup as local_backup

    with tempfile.TemporaryDirectory(prefix="qualia_backup_atomic_publish_") as tmp:
        root = Path(tmp)

        success_root = root / "success"
        success_root.mkdir()
        database, uploads, outputs, database_uri = build_fixture(success_root)
        backups = success_root / "backups"

        original_validate = local_backup.validate_backup
        original_sync_file = local_backup._sync_file_data
        original_replace = local_backup._atomic_replace_same_filesystem
        original_sync_directory = local_backup._sync_publish_directory
        validation_observation = {"called": False, "private": False, "official_absent": False}
        publish_events: list[tuple[str, str]] = []

        def observe_validation(path):
            candidate = Path(path)
            publish_events.append(("validate", candidate.name))
            validation_observation["called"] = True
            validation_observation["private"] = (
                candidate.parent == backups.resolve()
                and candidate.name.startswith(".qualia_backup_")
                and candidate.suffix == ".partial"
            )
            validation_observation["official_absent"] = not list(backups.glob("qualia_backup_*.zip"))
            return original_validate(candidate)

        def observe_sync_file(path):
            candidate = Path(path)
            publish_events.append(("sync_file", candidate.name))
            return original_sync_file(candidate)

        def observe_replace(source, destination):
            publish_events.append(("replace", f"{Path(source).name}->{Path(destination).name}"))
            return original_replace(Path(source), Path(destination))

        def observe_sync_directory(path):
            publish_events.append(("sync_directory", Path(path).name))
            return original_sync_directory(Path(path))

        local_backup.validate_backup = observe_validation
        local_backup._sync_file_data = observe_sync_file
        local_backup._atomic_replace_same_filesystem = observe_replace
        local_backup._sync_publish_directory = observe_sync_directory
        try:
            archive = local_backup.create_backup(
                backups,
                database_uri=database_uri,
                upload_dir=uploads,
                output_dir=outputs,
                label="atomic_success",
            )
        finally:
            local_backup.validate_backup = original_validate
            local_backup._sync_file_data = original_sync_file
            local_backup._atomic_replace_same_filesystem = original_replace
            local_backup._sync_publish_directory = original_sync_directory

        failures += check(
            "backup is validated while only the private partial archive exists",
            validation_observation["called"]
            and validation_observation["private"]
            and validation_observation["official_absent"],
            str(validation_observation),
        )
        failures += check(
            "validated backup is atomically published under the official ZIP name",
            archive.is_file()
            and archive.parent == backups.resolve()
            and archive.name.startswith("qualia_backup_")
            and archive.suffix == ".zip"
            and not list(backups.glob("*.partial"))
            and not list(backups.glob(".*.partial")),
            str(archive),
        )
        failures += check(
            "published backup remains fully valid",
            original_validate(archive).get("format") == "qualia-transcript-backup",
        )
        if os.name != "nt":
            archive_mode = stat.S_IMODE(archive.stat().st_mode)
            failures += check(
                "published backup remains owner-only on POSIX",
                (archive_mode & 0o077) == 0,
                oct(archive_mode),
            )

        event_names = [name for name, _detail in publish_events]
        try:
            validate_index = event_names.index("validate")
            first_sync_index = event_names.index("sync_file", validate_index + 1)
            replace_index = event_names.index("replace", first_sync_index + 1)
            if os.name == "nt":
                durability_index = event_names.index("sync_file", replace_index + 1)
            else:
                durability_index = event_names.index("sync_directory", replace_index + 1)
            durable_order = validate_index < first_sync_index < replace_index < durability_index
        except ValueError:
            durable_order = False
        failures += check(
            "validated bytes are flushed before rename and publication is durability-fenced before success",
            durable_order,
            str(publish_events),
        )

        validation_fail_root = root / "validation-failure"
        validation_fail_root.mkdir()
        database, uploads, outputs, database_uri = build_fixture(validation_fail_root)
        validation_fail_backups = validation_fail_root / "backups"

        def reject_validation(_path):
            raise ValueError("forced validation failure")

        local_backup.validate_backup = reject_validation
        validation_failed = False
        try:
            local_backup.create_backup(
                validation_fail_backups,
                database_uri=database_uri,
                upload_dir=uploads,
                output_dir=outputs,
                label="validation_failure",
            )
        except ValueError as exc:
            validation_failed = "forced validation failure" in str(exc)
        finally:
            local_backup.validate_backup = original_validate
        failures += check(
            "validation failure never publishes an official backup and cleans the partial",
            validation_failed
            and not list(validation_fail_backups.glob("qualia_backup_*.zip"))
            and not list(validation_fail_backups.glob("*.partial"))
            and not list(validation_fail_backups.glob(".*.partial")),
        )

        publish_fail_root = root / "publish-failure"
        publish_fail_root.mkdir()
        database, uploads, outputs, database_uri = build_fixture(publish_fail_root)
        publish_fail_backups = publish_fail_root / "backups"
        replace_seen = {"called": False, "validated": False}

        def fail_replace(source, destination):
            replace_seen["called"] = True
            replace_seen["validated"] = original_validate(source).get("format_version") == 1
            raise OSError("forced atomic publish failure")

        local_backup._atomic_replace_same_filesystem = fail_replace
        publish_failed = False
        try:
            local_backup.create_backup(
                publish_fail_backups,
                database_uri=database_uri,
                upload_dir=uploads,
                output_dir=outputs,
                label="publish_failure",
            )
        except OSError as exc:
            publish_failed = "forced atomic publish failure" in str(exc)
        finally:
            local_backup._atomic_replace_same_filesystem = original_replace
        failures += check(
            "publish failure occurs only after validation and leaves no official or partial artifact",
            publish_failed
            and replace_seen["called"]
            and replace_seen["validated"]
            and not list(publish_fail_backups.glob("qualia_backup_*.zip"))
            and not list(publish_fail_backups.glob("*.partial"))
            and not list(publish_fail_backups.glob(".*.partial")),
            str(replace_seen),
        )

        post_rename_fail_root = root / "post-rename-failure"
        post_rename_fail_root.mkdir()
        database, uploads, outputs, database_uri = build_fixture(post_rename_fail_root)
        post_rename_backups = post_rename_fail_root / "backups"
        original_publish = local_backup._publish_validated_archive

        def fail_after_rename(partial, archive_path):
            os.replace(partial, archive_path)
            raise OSError("forced post-rename durability failure")

        local_backup._publish_validated_archive = fail_after_rename
        post_rename_failed = False
        try:
            local_backup.create_backup(
                post_rename_backups,
                database_uri=database_uri,
                upload_dir=uploads,
                output_dir=outputs,
                label="post_rename_failure",
            )
        except OSError as exc:
            post_rename_failed = "forced post-rename durability failure" in str(exc)
        finally:
            local_backup._publish_validated_archive = original_publish
        failures += check(
            "a durability failure after rename removes the not-successfully-published official artifact",
            post_rename_failed
            and not list(post_rename_backups.glob("qualia_backup_*.zip"))
            and not list(post_rename_backups.glob("*.partial"))
            and not list(post_rename_backups.glob(".*.partial")),
        )

        source_text = (repo_root / "services" / "local_backup.py").read_text(encoding="utf-8")
        failures += check(
            "backup implementation crash-durably publishes only validated same-filesystem bytes",
            "validate_backup(partial_archive)" in source_text
            and "_publish_validated_archive(partial_archive, archive)" in source_text
            and "_sync_file_data(partial)" in source_text
            and "_atomic_replace_same_filesystem(partial, archive)" in source_text
            and "_sync_publish_directory(archive.parent)" in source_text
            and "movefile_write_through" in source_text
            and "partial_archive.unlink(missing_ok=True)" in source_text
            and "archive.unlink(missing_ok=True)" in source_text
            and "zipfile.ZipFile(partial_archive" in source_text,
        )
        failures += check(
            "Windows backup privacy is enforced with a protected owner SID ACL before partial creation",
            "whoami\", \"/user\", \"/fo\", \"csv\", \"/nh" in source_text
            and "SetAccessRuleProtection($true, $false)" in source_text
            and "RemoveAccessRuleSpecific" in source_text
            and "FileSystemAccessRule" in source_text
            and "_restrict_permissions(destination, 0o700)" in source_text,
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())