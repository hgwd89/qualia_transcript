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
        failures += check(
            f"{relative} uses only the combined generated-output boundary",
            "write_and_register_generated_file" in calls
            and "write_output_target" not in calls
            and "register_generated_file" not in calls,
            f"calls={sorted(calls & {'write_and_register_generated_file', 'write_output_target', 'register_generated_file'})}",
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
        "formal generated output is fsynced and identity-verified before DB commit",
        "os.fsync(opened.stream.fileno())" in manager_source
        and "_verify_current_target(target, written)" in manager_source
        and manager_source.index("_verify_current_target(target, written)")
        < manager_source.index("db.session.commit()"),
    )

    original_open = write_boundary.os.open
    original_supports = write_boundary._supports_pinned_posix_write
    supports_pinned = original_supports()

    if supports_pinned:
        with tempfile.TemporaryDirectory(prefix="qualia_output_write_fence_") as tmp:
            root = Path(tmp)
            output_root = root / "outputs"
            output_root.mkdir()

            # Successful write: replace the visible ID directory immediately before
            # final-file creation. The openat call must still target the pinned
            # original parent rather than the replacement pathname.
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

            # Restore the first fixture before exercising abort cleanup.
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
            "POSIX generated-output race regression is skipped only on unsupported platforms",
            os.name == "nt" or not supports_pinned,
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
