import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def raises_value_error(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def read_and_close(fn) -> bytes:
    handle = fn()
    try:
        return handle.read()
    finally:
        handle.close()


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    import services.storage_paths as storage_paths
    from services.file_manager import prepare_output_target
    from services.upload_manager import prepare_media_upload_target

    original_output = config.OUTPUT_DIR
    original_upload = config.UPLOAD_DIR
    original_detector = storage_paths.is_link_or_reparse
    original_os_open = storage_paths.os.open

    with tempfile.TemporaryDirectory(prefix="qualia_link_guard_") as tmp:
        root = Path(tmp)
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")
        try:
            output = prepare_output_target(1, "正常出力.xlsx")
            media = prepare_media_upload_target(2, "面接音声.mp3")
            failures += check(
                "normal managed ID directories remain usable",
                Path(output.full_path).parent == Path(config.OUTPUT_DIR).resolve() / "1"
                and Path(media.full_path).parent == Path(config.UPLOAD_DIR).resolve() / "2",
            )

            output_path = Path(output.full_path)
            output_path.write_bytes(b"inside-managed-file")
            failures += check(
                "managed reader returns the expected regular file",
                read_and_close(
                    lambda: storage_paths.open_managed_file_for_read(
                        config.OUTPUT_DIR,
                        output.stored_path,
                    )
                )
                == b"inside-managed-file",
            )

            outside = root / "outside.bin"
            outside.write_bytes(b"different-file-object")

            def redirected_open(path, flags, *args, **kwargs):
                if Path(path) == output_path:
                    return original_os_open(outside, flags, *args, **kwargs)
                return original_os_open(path, flags, *args, **kwargs)

            storage_paths.os.open = redirected_open
            failures += check(
                "managed reader rejects an opened descriptor that differs from the checked path",
                raises_value_error(
                    lambda: read_and_close(
                        lambda: storage_paths.open_managed_file_for_read(
                            config.OUTPUT_DIR,
                            output.stored_path,
                        )
                    )
                ),
            )
            storage_paths.os.open = original_os_open

            def fake_link_detector(path: Path) -> bool:
                return path.name in {"7", "8", "linked.xlsx"}

            storage_paths.is_link_or_reparse = fake_link_detector
            failures += check(
                "output target rejects linked/reparse project directory",
                raises_value_error(lambda: prepare_output_target(7, "report.xlsx")),
            )
            failures += check(
                "media target rejects linked/reparse interview directory",
                raises_value_error(lambda: prepare_media_upload_target(8, "audio.mp3")),
            )

            linked_dir = Path(config.OUTPUT_DIR).resolve() / "9"
            linked_dir.mkdir(parents=True, exist_ok=True)
            (linked_dir / "linked.xlsx").write_bytes(b"x")
            failures += check(
                "managed resolver rejects linked/reparse stored file",
                raises_value_error(
                    lambda: storage_paths.resolve_managed_path(
                        config.OUTPUT_DIR,
                        "9/linked.xlsx",
                    )
                ),
            )
        finally:
            storage_paths.os.open = original_os_open
            storage_paths.is_link_or_reparse = original_detector
            config.OUTPUT_DIR = original_output
            config.UPLOAD_DIR = original_upload

    helper_source = (repo_root / "services" / "storage_paths.py").read_text(encoding="utf-8")
    failures += check(
        "Windows reparse-point detection is part of the guard",
        "FILE_ATTRIBUTE_REPARSE_POINT" in helper_source
        and "path.is_symlink()" in helper_source,
    )
    failures += check(
        "managed reader validates the opened descriptor against the current path",
        "os.fstat(fd)" in helper_source
        and "lexical.lstat()" in helper_source
        and "managed storage file changed during open" in helper_source,
    )

    route_source = (repo_root / "routes" / "outputs.py").read_text(encoding="utf-8")
    failures += check(
        "output download streams from the descriptor-fenced managed reader",
        "open_managed_file_for_read(config.OUTPUT_DIR, gf.stored_path)" in route_source
        and "response.call_on_close(file_obj.close)" in route_source
        and "os.path.isfile(full_path)" not in route_source,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
