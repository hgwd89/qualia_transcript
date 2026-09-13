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
        validation_observation = {"called": False, "private": False, "official_absent": False}

        def observe_validation(path):
            candidate = Path(path)
            validation_observation["called"] = True
            validation_observation["private"] = (
                candidate.parent == backups.resolve()
                and candidate.name.startswith(".qualia_backup_")
                and candidate.suffix == ".partial"
            )
            validation_observation["official_absent"] = not list(backups.glob("qualia_backup_*.zip"))
            return original_validate(candidate)

        local_backup.validate_backup = observe_validation
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
        original_replace = local_backup.os.replace
        replace_seen = {"called": False, "validated": False}

        def fail_replace(source, destination):
            replace_seen["called"] = True
            replace_seen["validated"] = original_validate(source).get("format_version") == 1
            raise OSError("forced atomic publish failure")

        local_backup.os.replace = fail_replace
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
            local_backup.os.replace = original_replace
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

        source_text = (repo_root / "services" / "local_backup.py").read_text(encoding="utf-8")
        failures += check(
            "backup implementation publishes only after validation using same-filesystem os.replace",
            "validate_backup(partial_archive)" in source_text
            and "os.replace(partial_archive, archive)" in source_text
            and "partial_archive.unlink(missing_ok=True)" in source_text
            and "zipfile.ZipFile(partial_archive" in source_text,
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())