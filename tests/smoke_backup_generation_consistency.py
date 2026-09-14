import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def create_fixture(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO marker(value) VALUES ('v1')")
        con.commit()
    finally:
        con.close()


def pinned_connection(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN")
    con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    return con


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from services.local_backup import create_backup, validate_backup
    import services.readiness_backup_freshness as freshness

    failures = 0
    with tempfile.TemporaryDirectory(prefix="qualia_backup_generation_consistency_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        upload_dir = root / "uploads"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        upload_dir.mkdir()
        output_dir.mkdir()
        backup_dir.mkdir()
        create_fixture(db_path)
        (upload_dir / "source.bin").write_bytes(b"source-v1")
        (output_dir / "report.txt").write_text("report-v1", encoding="utf-8")

        initial_archive = create_backup(
            backup_dir,
            database_uri=f"sqlite:///{db_path.as_posix()}",
            upload_dir=upload_dir,
            output_dir=output_dir,
            label="generation_initial",
        )
        stable_archive_bytes = initial_archive.read_bytes()

        con = pinned_connection(db_path)
        try:
            baseline = freshness.compare_latest_backup_to_current_recovery_set(
                con, backup_dir, upload_dir, output_dir
            )
        finally:
            con.rollback()
            con.close()
        failures += check(
            "stable newest backup is accepted before race injection",
            baseline.matches_current is True
            and baseline.validation_error is None
            and baseline.comparison_error is None,
            str(baseline),
        )

        original_current_files = freshness._current_recovery_files

        def mutate_selected_archive(connection, upload_root, output_root):
            files = original_current_files(connection, upload_root, output_root)
            mutated = bytearray(stable_archive_bytes)
            mutated[-1] ^= 0x01
            initial_archive.write_bytes(bytes(mutated))
            return files

        freshness._current_recovery_files = mutate_selected_archive
        try:
            con = pinned_connection(db_path)
            try:
                changed_generation = freshness.compare_latest_backup_to_current_recovery_set(
                    con, backup_dir, upload_dir, output_dir
                )
            finally:
                con.rollback()
                con.close()
        finally:
            freshness._current_recovery_files = original_current_files
            initial_archive.write_bytes(stable_archive_bytes)
        validate_backup(initial_archive)
        failures += check(
            "in-place backup replacement after validation fails closed",
            changed_generation.matches_current is None
            and changed_generation.validation_error is None
            and changed_generation.comparison_error is not None
            and "archive changed during readiness comparison" in changed_generation.comparison_error,
            str(changed_generation),
        )

        racing_archive = backup_dir / "qualia_backup_99991231T235959Z_race_ffffffff.zip"

        def publish_newer_archive(connection, upload_root, output_root):
            files = original_current_files(connection, upload_root, output_root)
            shutil.copyfile(initial_archive, racing_archive)
            return files

        freshness._current_recovery_files = publish_newer_archive
        try:
            con = pinned_connection(db_path)
            try:
                changed_selection = freshness.compare_latest_backup_to_current_recovery_set(
                    con, backup_dir, upload_dir, output_dir
                )
            finally:
                con.rollback()
                con.close()
        finally:
            freshness._current_recovery_files = original_current_files
            racing_archive.unlink(missing_ok=True)
        failures += check(
            "newer backup publication during comparison fails closed",
            changed_selection.matches_current is None
            and changed_selection.validation_error is None
            and changed_selection.comparison_error is not None
            and "selection changed during readiness comparison" in changed_selection.comparison_error,
            str(changed_selection),
        )

        con = pinned_connection(db_path)
        try:
            restored = freshness.compare_latest_backup_to_current_recovery_set(
                con, backup_dir, upload_dir, output_dir
            )
        finally:
            con.rollback()
            con.close()
        failures += check(
            "stable archive is accepted again after race fixtures are removed",
            restored.matches_current is True
            and restored.validation_error is None
            and restored.comparison_error is None,
            str(restored),
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
