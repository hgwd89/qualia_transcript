import errno
import os
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def xattr_unsupported(exc: OSError) -> bool:
    return exc.errno in {
        getattr(errno, "ENOTSUP", -1),
        getattr(errno, "EOPNOTSUPP", -2),
        getattr(errno, "ENOSYS", -3),
        getattr(errno, "EPERM", -4),
    }


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import services.local_backup as local_backup

    with tempfile.TemporaryDirectory(prefix="qualia_snapshot_fencing_") as tmp:
        root = Path(tmp)

        reuse_source = root / "inode-reuse-source.txt"
        reuse_target = root / "inode-reuse-target.txt"
        reuse_source.write_text("original", encoding="utf-8")
        original_open = local_backup.os.open
        replacement = {"done": False, "same_inode": False}

        def replace_before_open(path, flags, *args, **kwargs):
            if Path(path) == reuse_source and not replacement["done"]:
                before = reuse_source.stat()
                reuse_source.unlink()
                reuse_source.write_text("replaced", encoding="utf-8")
                after = reuse_source.stat()
                replacement["done"] = True
                replacement["same_inode"] = (
                    before.st_dev == after.st_dev and before.st_ino == after.st_ino
                )
            return original_open(path, flags, *args, **kwargs)

        local_backup.os.open = replace_before_open
        replacement_rejected = False
        try:
            local_backup._copy_regular_snapshot_file(reuse_source, reuse_target)
        except ValueError as exc:
            replacement_rejected = "changed during snapshot" in str(exc)
        finally:
            local_backup.os.open = original_open
        failures += check(
            "snapshot rejects unlink/recreate replacement even when inode reuse is possible",
            replacement["done"] and replacement_rejected and not reuse_target.exists(),
            f"same_inode={replacement['same_inode']}",
        )

        if local_backup._supports_pinned_posix_walk():
            source = root / "ancestor-source"
            held = root / "ancestor-held"
            external = root / "ancestor-external"
            target = root / "ancestor-target"
            inner = source / "inner"
            inner.mkdir(parents=True)
            external.mkdir()
            (inner / "payload.txt").write_text("inside-root", encoding="utf-8")
            (external / "payload.txt").write_text("outside-root", encoding="utf-8")

            original_open = local_backup.os.open
            swapped = {"done": False}

            def replace_ancestor_before_leaf_open(path, flags, *args, **kwargs):
                if (
                    path == "payload.txt"
                    and kwargs.get("dir_fd") is not None
                    and not swapped["done"]
                ):
                    inner.rename(held)
                    inner.symlink_to(external, target_is_directory=True)
                    swapped["done"] = True
                return original_open(path, flags, *args, **kwargs)

            local_backup.os.open = replace_ancestor_before_leaf_open
            try:
                local_backup._snapshot_tree_no_links(source, target)
            finally:
                local_backup.os.open = original_open
            copied = target / "inner" / "payload.txt"
            failures += check(
                "POSIX rollback snapshot stays on the pinned ancestor after pathname replacement",
                swapped["done"]
                and copied.is_file()
                and copied.read_text(encoding="utf-8") == "inside-root",
                copied.read_text(encoding="utf-8") if copied.is_file() else "missing",
            )

            collect_source = root / "collect-source"
            collect_held = root / "collect-held"
            collect_external = root / "collect-external"
            collect_stage = root / "collect-stage"
            collect_inner = collect_source / "inner"
            collect_inner.mkdir(parents=True)
            collect_external.mkdir()
            (collect_inner / "payload.txt").write_text("collect-inside", encoding="utf-8")
            (collect_external / "payload.txt").write_text("collect-outside", encoding="utf-8")
            swapped_collect = {"done": False}

            original_open = local_backup.os.open

            def replace_collect_ancestor(path, flags, *args, **kwargs):
                if (
                    path == "payload.txt"
                    and kwargs.get("dir_fd") is not None
                    and not swapped_collect["done"]
                ):
                    collect_inner.rename(collect_held)
                    collect_inner.symlink_to(collect_external, target_is_directory=True)
                    swapped_collect["done"] = True
                return original_open(path, flags, *args, **kwargs)

            local_backup.os.open = replace_collect_ancestor
            try:
                entries = local_backup._collect_tree(collect_source, "uploads", collect_stage)
            finally:
                local_backup.os.open = original_open
            staged = collect_stage / "uploads" / "inner" / "payload.txt"
            failures += check(
                "POSIX backup collection also stays beneath pinned directory descriptors",
                swapped_collect["done"]
                and staged.is_file()
                and staged.read_text(encoding="utf-8") == "collect-inside"
                and [entry["path"] for entry in entries] == ["uploads/inner/payload.txt"],
                staged.read_text(encoding="utf-8") if staged.is_file() else "missing",
            )
        else:
            failures += check(
                "POSIX pinned-directory race regression",
                True,
                "SKIP: descriptor-relative POSIX walk unavailable on this platform",
            )

        if (
            os.name != "nt"
            and hasattr(os, "setxattr")
            and hasattr(os, "getxattr")
            and hasattr(os, "listxattr")
        ):
            meta_source = root / "xattr-source"
            meta_target = root / "xattr-target"
            meta_source.mkdir()
            meta_file = meta_source / "payload.txt"
            meta_file.write_text("xattr-payload", encoding="utf-8")
            attr_name = "user.qualia_snapshot_test"
            xattr_available = True
            try:
                os.setxattr(meta_source, attr_name, b"directory-value")
                os.setxattr(meta_file, attr_name, b"file-value")
            except OSError as exc:
                if xattr_unsupported(exc):
                    xattr_available = False
                else:
                    raise

            if xattr_available:
                local_backup._snapshot_tree_no_links(meta_source, meta_target)
                failures += check(
                    "rollback snapshot preserves directory and file extended attributes",
                    os.getxattr(meta_target, attr_name) == b"directory-value"
                    and os.getxattr(meta_target / "payload.txt", attr_name) == b"file-value",
                )
            else:
                failures += check(
                    "rollback snapshot extended-attribute regression",
                    True,
                    "SKIP: temporary filesystem does not support user xattrs",
                )
        else:
            failures += check(
                "rollback snapshot extended-attribute regression",
                True,
                "SKIP: xattr API unavailable on this platform",
            )

        source_text = (repo_root / "services" / "local_backup.py").read_text(encoding="utf-8")
        failures += check(
            "implementation pins POSIX ancestry and carries generation-sensitive metadata",
            "dir_fd=parent_fd" in source_text
            and "os.O_DIRECTORY | os.O_NOFOLLOW" in source_text
            and "st_ctime_ns" in source_text
            and "_capture_extended_attributes" in source_text
            and "_apply_extended_attributes" in source_text,
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
