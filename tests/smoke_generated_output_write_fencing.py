import ast
import os
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def called_functions(source: str) -> set[str]:
    tree = ast.parse(source)
    result: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            result.add(func.id)
        elif isinstance(func, ast.Attribute):
            result.add(func.attr)
    return result


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    import services.managed_storage_write as write_boundary

    formal_reports = [
        "services/report_verbatim.py",
        "services/report_formatted.py",
        "services/report_analysis.py",
        "services/report_approved_analysis.py",
    ]
    for relative in formal_reports:
        source = (repo_root / relative).read_text(encoding="utf-8")
        calls = called_functions(source)
        relevant = {
            name for name in calls
            if name in {
                "write_and_register_generated_file",
                "write_output_target",
                "register_generated_file",
            }
        }
        failures += check(
            f"{relative} uses only the combined generated-output boundary",
            "write_and_register_generated_file" in calls
            and "write_output_target" not in calls
            and "register_generated_file" not in calls,
            f"calls={sorted(relevant)}",
        )

    boundary_source = (repo_root / "services" / "managed_storage_write.py").read_text(
        encoding="utf-8"
    )
    manager_source = (repo_root / "services" / "file_manager.py").read_text(
        encoding="utf-8"
    )
    failures += check(
        "POSIX managed writes use exclusive no-follow descriptor-relative creation",
        "os.O_EXCL" in boundary_source
        and "os.O_NOFOLLOW" in boundary_source
        and "dir_fd=parent_fd" in boundary_source,
    )
    failures += check(
        "Windows managed writes pin ancestors and create a new non-reparse file",
        "create_new = 1" in boundary_source
        and "file_flag_open_reparse_point = 0x00200000" in boundary_source
        and "_open_windows_directory_chain" in boundary_source
        and "file_share_read = 0x00000001" in boundary_source,
    )
    failures += check(
        "formal generated output is fsynced and verified before DB commit",
        "os.fsync(opened.stream.fileno())" in manager_source
        and "_verify_current_target(target, written)" in manager_source
        and manager_source.index("_verify_current_target(target, written)")
        < manager_source.index("db.session.commit()"),
    )

    # Exercise actual create/finish/abort behavior on every supported CI platform.
    with tempfile.TemporaryDirectory(prefix="qualia_output_write_basic_") as tmp:
        root = Path(tmp)
        managed_root = root / "outputs"
        id_dir = managed_root / "1"
        id_dir.mkdir(parents=True)

        opened = write_boundary.open_managed_file_for_write(managed_root, "1/basic.bin")
        opened.stream.write(b"managed-write")
        opened.finish()
        failures += check(
            "managed write creates the expected regular file",
            (id_dir / "basic.bin").read_bytes() == b"managed-write",
        )

        aborted = write_boundary.open_managed_file_for_write(managed_root, "1/abort.bin")
        aborted.stream.write(b"partial")
        aborted.abort()
        failures += check(
            "managed write abort removes the exact partial file",
            not (id_dir / "abort.bin").exists(),
        )

    # Exercise the combined filesystem + DB registration boundary on Windows and
    # POSIX using only temporary application storage.
    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }
    with tempfile.TemporaryDirectory(prefix="qualia_output_write_db_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'generated.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")
        try:
            from app import create_app
            from models import db
            from models.generated_file import GeneratedFile
            from models.project import Project
            from services.file_manager import (
                prepare_output_target,
                write_and_register_generated_file,
            )

            app = create_app()
            app.config["TESTING"] = True
            with app.app_context():
                project = Project(name="write-fencing-smoke")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)
                target = prepare_output_target(project_id, "fenced.bin")
                gf = write_and_register_generated_file(
                    target,
                    lambda stream: stream.write(b"registered-write"),
                    project_id=project_id,
                    file_type="analysis",
                    file_format="bin",
                )
                failures += check(
                    "combined write/register boundary commits matching bytes and DB metadata",
                    gf.id is not None
                    and GeneratedFile.query.filter_by(id=gf.id).count() == 1
                    and Path(target.full_path).read_bytes() == b"registered-write",
                    f"file_id={gf.id}",
                )
                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]

    original_open = write_boundary.os.open
    original_supports = write_boundary._supports_pinned_posix_write
    supports_pinned = original_supports()

    if supports_pinned:
        with tempfile.TemporaryDirectory(prefix="qualia_output_write_fence_") as tmp:
            root = Path(tmp)
            output_root = root / "outputs"
            output_root.mkdir()

            id_dir = output_root / "7"
            id_dir.mkdir()
            saved_dir = output_root / "7.pinned-original"
            filename = "race.bin"
            swapped = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if (
                    not swapped
                    and kwargs.get("dir_fd") is not None
                    and str(path) == filename
                    and bool(flags & os.O_CREAT)
                    and not bool(flags & getattr(os, "O_DIRECTORY", 0))
                ):
                    id_dir.rename(saved_dir)
                    id_dir.mkdir()
                    swapped = True
                return original_open(path, flags, *args, **kwargs)

            write_boundary._supports_pinned_posix_write = lambda: True
            write_boundary.os.open = racing_open
            try:
                opened = write_boundary.open_managed_file_for_write(
                    output_root,
                    f"7/{filename}",
                )
                opened.stream.write(b"pinned-write")
                opened.finish()
            finally:
                write_boundary.os.open = original_open
                write_boundary._supports_pinned_posix_write = original_supports

            failures += check(
                "POSIX generated write stays on pinned parent after pathname replacement",
                swapped
                and (saved_dir / filename).read_bytes() == b"pinned-write"
                and not (id_dir / filename).exists(),
                f"swapped={swapped}",
            )

            id_dir.rmdir()
            saved_dir.rename(id_dir)

            abort_dir = output_root / "8"
            abort_dir.mkdir()
            abort_saved = output_root / "8.pinned-original"
            abort_name = "abort.bin"
            abort_swapped = False

            def racing_abort_open(path, flags, *args, **kwargs):
                nonlocal abort_swapped
                if (
                    not abort_swapped
                    and kwargs.get("dir_fd") is not None
                    and str(path) == abort_name
                    and bool(flags & os.O_CREAT)
                    and not bool(flags & getattr(os, "O_DIRECTORY", 0))
                ):
                    abort_dir.rename(abort_saved)
                    abort_dir.mkdir()
                    (abort_dir / abort_name).write_bytes(b"replacement-must-survive")
                    abort_swapped = True
                return original_open(path, flags, *args, **kwargs)

            write_boundary._supports_pinned_posix_write = lambda: True
            write_boundary.os.open = racing_abort_open
            try:
                opened = write_boundary.open_managed_file_for_write(
                    output_root,
                    f"8/{abort_name}",
                )
                opened.stream.write(b"partial")
                opened.abort()
            finally:
                write_boundary.os.open = original_open
                write_boundary._supports_pinned_posix_write = original_supports

            failures += check(
                "POSIX abort removes only the pinned partial after pathname replacement",
                abort_swapped
                and not (abort_saved / abort_name).exists()
                and (abort_dir / abort_name).read_bytes() == b"replacement-must-survive",
                f"swapped={abort_swapped}",
            )
    else:
        failures += check(
            "POSIX generated-output race regression skips only where unavailable",
            os.name == "nt" or not supports_pinned,
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
