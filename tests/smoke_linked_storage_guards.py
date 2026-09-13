import os
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
    opened = fn()
    try:
        return opened.stream.read()
    finally:
        opened.close()


def write_and_close(fn, data: bytes) -> None:
    opened = fn()
    try:
        opened.stream.write(data)
        opened.stream.flush()
    finally:
        opened.close()


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
    original_supports_pinned = storage_paths._supports_pinned_posix_read
    supports_pinned_posix = original_supports_pinned()

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

            if supports_pinned_posix:
                race_target = prepare_output_target(3, "race.bin")
                race_path = Path(race_target.full_path)
                race_path.write_bytes(b"pinned-inside")
                outside_dir = root / "outside-tree"
                outside_dir.mkdir()
                (outside_dir / race_path.name).write_bytes(b"outside-redirection")
                id_dir = race_path.parent
                saved_dir = id_dir.with_name(f"{id_dir.name}.pinned-original")
                swapped = False

                def racing_open(path, flags, *args, **kwargs):
                    nonlocal swapped
                    if (
                        not swapped
                        and kwargs.get("dir_fd") is not None
                        and str(path) == race_path.name
                        and not (flags & getattr(os, "O_DIRECTORY", 0))
                    ):
                        id_dir.rename(saved_dir)
                        os.symlink(outside_dir, id_dir, target_is_directory=True)
                        swapped = True
                    return original_os_open(path, flags, *args, **kwargs)

                # Monkeypatching storage_paths.os.open mutates the shared os module,
                # so preserve the capability decision made with the real os.open.
                storage_paths._supports_pinned_posix_read = lambda: supports_pinned_posix
                storage_paths.os.open = racing_open
                try:
                    raced_data = read_and_close(
                        lambda: storage_paths.open_managed_file_for_read(
                            config.OUTPUT_DIR,
                            race_target.stored_path,
                        )
                    )
                finally:
                    storage_paths.os.open = original_os_open
                    storage_paths._supports_pinned_posix_read = original_supports_pinned
                    if id_dir.is_symlink():
                        id_dir.unlink()
                    if saved_dir.exists():
                        saved_dir.rename(id_dir)
                failures += check(
                    "POSIX managed reader stays on pinned parent after pathname replacement",
                    swapped and raced_data == b"pinned-inside",
                    f"swapped={swapped} data={raced_data!r}",
                )

                write_target = prepare_output_target(4, "write-race.bin")
                write_path = Path(write_target.full_path)
                write_outside = root / "outside-write-tree"
                write_outside.mkdir()
                write_id_dir = write_path.parent
                write_saved_dir = write_id_dir.with_name(
                    f"{write_id_dir.name}.pinned-write-original"
                )
                write_swapped = False

                def racing_create(path, flags, *args, **kwargs):
                    nonlocal write_swapped
                    if (
                        not write_swapped
                        and kwargs.get("dir_fd") is not None
                        and str(path) == write_path.name
                        and bool(flags & getattr(os, "O_CREAT", 0))
                    ):
                        write_id_dir.rename(write_saved_dir)
                        os.symlink(write_outside, write_id_dir, target_is_directory=True)
                        write_swapped = True
                    return original_os_open(path, flags, *args, **kwargs)

                storage_paths._supports_pinned_posix_read = lambda: supports_pinned_posix
                storage_paths.os.open = racing_create
                try:
                    write_and_close(
                        lambda: storage_paths.open_managed_file_for_create(
                            config.OUTPUT_DIR,
                            write_target.stored_path,
                        ),
                        b"pinned-write",
                    )
                    pinned_written = (write_saved_dir / write_path.name).read_bytes()
                    redirected_exists = (write_outside / write_path.name).exists()
                finally:
                    storage_paths.os.open = original_os_open
                    storage_paths._supports_pinned_posix_read = original_supports_pinned
                    if write_id_dir.is_symlink():
                        write_id_dir.unlink()
                    if write_saved_dir.exists():
                        write_saved_dir.rename(write_id_dir)
                failures += check(
                    "POSIX managed writer creates beneath pinned parent after pathname replacement",
                    write_swapped
                    and pinned_written == b"pinned-write"
                    and not redirected_exists,
                    (
                        f"swapped={write_swapped} data={pinned_written!r} "
                        f"redirected={redirected_exists}"
                    ),
                )

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
            storage_paths._supports_pinned_posix_read = original_supports_pinned
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
        "POSIX managed reads and creates descend through pinned no-follow descriptors",
        "dir_fd=parent_fd" in helper_source
        and "os.O_NOFOLLOW" in helper_source
        and "os.O_EXCL" in helper_source
        and "_open_posix_directory_chain" in helper_source
        and "open_managed_file_for_create" in helper_source,
    )
    failures += check(
        "Windows managed reads and creates deny write/delete sharing while pinning components",
        "file_flag_open_reparse_point = 0x00200000" in helper_source
        and "file_share_read = 0x00000001" in helper_source
        and "file_share_write =" not in helper_source
        and "file_share_delete =" not in helper_source
        and "create_new = 1" in helper_source
        and "_windows_create_file_handle" in helper_source
        and "_open_windows_directory_chain" in helper_source,
    )

    writer_paths = [
        "services/report_verbatim.py",
        "services/report_formatted.py",
        "services/report_analysis.py",
        "services/report_approved_analysis.py",
    ]
    writer_sources = {
        path: (repo_root / path).read_text(encoding="utf-8")
        for path in writer_paths
    }
    failures += check(
        "all generated-output writers use the pinned create boundary",
        all(
            "open_output_target_for_write" in source
            and ".save(target.full_path)" not in source
            and "open(target.full_path" not in source
            for source in writer_sources.values()
        ),
        ", ".join(
            path
            for path, source in writer_sources.items()
            if "open_output_target_for_write" not in source
            or ".save(target.full_path)" in source
            or "open(target.full_path" in source
        ),
    )

    upload_source = (repo_root / "services" / "upload_manager.py").read_text(encoding="utf-8")
    failures += check(
        "media upload writes through the pinned create boundary",
        "open_managed_file_for_create(config.UPLOAD_DIR, target.stored_path)" in upload_source
        and "file_storage.save(opened.stream)" in upload_source
        and "file_storage.save(target.full_path)" not in upload_source,
    )

    route_source = (repo_root / "routes" / "outputs.py").read_text(encoding="utf-8")
    failures += check(
        "output download streams from the pinned managed reader",
        "open_managed_file_for_read(config.OUTPUT_DIR, gf.stored_path)" in route_source
        and "response.call_on_close(opened.close)" in route_source
        and "response.make_conditional(" in route_source
        and "os.path.isfile(full_path)" not in route_source,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
