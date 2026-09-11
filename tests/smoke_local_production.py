import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path


FILE_CONTENTS = {
    "uploads/1/audio.txt": b"original-audio-fixture",
    "outputs/raw_transcripts/transcription_1.json": b'{"text":"raw original"}',
    "outputs/1/report.txt": b"generated-output-original",
}


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def write_fixture_tree(root: Path) -> tuple[Path, Path]:
    uploads = root / "uploads"
    outputs = root / "outputs"
    for rel, content in FILE_CONTENTS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return uploads, outputs


def create_db(path: Path, value: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE source_data (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO source_data(id, value) VALUES (1, ?)", (value,))
        con.commit()
    finally:
        con.close()


def db_value(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT value FROM source_data WHERE id=1").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    from services.local_backup import create_backup, restore_backup, validate_backup

    failures = 0
    if "APP_DEBUG" not in os.environ:
        failures += check("debug defaults off", config.APP_DEBUG is False, str(config.APP_DEBUG))
    if "APP_HOST" not in os.environ:
        failures += check("host defaults to localhost", config.APP_HOST == "127.0.0.1", config.APP_HOST)
    failures += check("predictable dev secret removed", config.SECRET_KEY != "dev-secret-key")

    launcher = (repo_root / "start_app.ps1").read_text(encoding="utf-8")
    failures += check("launcher has no user-specific absolute path", "C:\\Users\\" not in launcher)
    failures += check("launcher derives project root from script", "$PSScriptRoot" in launcher)

    app_source = (repo_root / "app.py").read_text(encoding="utf-8")
    failures += check("app runtime uses configured host", "host=config.APP_HOST" in app_source)
    failures += check("app runtime uses configured debug", "debug=config.APP_DEBUG" in app_source)
    failures += check("flask reloader disabled", "use_reloader=False" in app_source)

    with tempfile.TemporaryDirectory(prefix="qualia_backup_restore_smoke_") as tmp:
        root = Path(tmp)
        db_path = root / "smoke.db"
        uploads, outputs = write_fixture_tree(root)
        backup_dir = root / "backups"
        create_db(db_path, "original-db-value")
        database_uri = f"sqlite:///{db_path.as_posix()}"

        try:
            archive = create_backup(
                backup_dir,
                database_uri=database_uri,
                upload_dir=uploads,
                output_dir=outputs,
                label="smoke",
            )
            failures += check("backup archive created", archive.is_file(), str(archive))
            manifest = validate_backup(archive)
            failures += check(
                "backup contains database and all fixture files",
                len(manifest.get("files") or []) == 1 + len(FILE_CONTENTS),
                str(len(manifest.get("files") or [])),
            )

            validate_only = restore_backup(
                archive,
                apply=False,
                database_uri=database_uri,
                upload_dir=uploads,
                output_dir=outputs,
                backup_dir=backup_dir,
            )
            failures += check(
                "restore defaults to validation-only",
                validate_only.get("validated") is True and validate_only.get("restored") is False,
            )

            con = sqlite3.connect(db_path)
            try:
                con.execute("UPDATE source_data SET value='mutated-db-value' WHERE id=1")
                con.commit()
            finally:
                con.close()
            shutil.rmtree(uploads)
            shutil.rmtree(outputs)
            uploads.mkdir(parents=True)
            outputs.mkdir(parents=True)
            (uploads / "wrong.txt").write_text("wrong", encoding="utf-8")
            (outputs / "wrong.txt").write_text("wrong", encoding="utf-8")

            restored = restore_backup(
                archive,
                apply=True,
                database_uri=database_uri,
                upload_dir=uploads,
                output_dir=outputs,
                backup_dir=backup_dir,
                create_pre_restore_backup=False,
            )
            failures += check("restore reports success", restored.get("restored") is True)
            failures += check("database round-trip restored", db_value(db_path) == "original-db-value")

            for rel, content in FILE_CONTENTS.items():
                relative = Path(rel)
                if relative.parts[0] == "uploads":
                    path = uploads.joinpath(*relative.parts[1:])
                else:
                    path = outputs.joinpath(*relative.parts[1:])
                failures += check(f"file restored: {rel}", path.is_file() and path.read_bytes() == content)
            failures += check("stale upload removed on restore", not (uploads / "wrong.txt").exists())
            failures += check("stale output removed on restore", not (outputs / "wrong.txt").exists())

            corrupt = root / "corrupt.zip"
            with zipfile.ZipFile(archive, "r") as src, zipfile.ZipFile(corrupt, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    data = src.read(info.filename)
                    if info.filename == "outputs/1/report.txt":
                        data = b"tampered"
                    dst.writestr(info, data)
            corruption_blocked = False
            try:
                validate_backup(corrupt)
            except ValueError:
                corruption_blocked = True
            failures += check("tampered backup is rejected", corruption_blocked)

            extra = root / "extra-file.zip"
            with zipfile.ZipFile(archive, "r") as src, zipfile.ZipFile(extra, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    dst.writestr(info, src.read(info.filename))
                dst.writestr("outputs/unmanifested.txt", b"not declared")
            extra_blocked = False
            try:
                validate_backup(extra)
            except ValueError:
                extra_blocked = True
            failures += check("unmanifested archive file is rejected", extra_blocked)

        except Exception as exc:
            failures += check("backup/restore smoke", False, f"{type(exc).__name__}: {exc}")

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
