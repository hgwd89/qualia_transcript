import errno
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

try:
    import resource
except ImportError:  # Windows
    resource = None


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


def windows_acl_sids(path: Path) -> set[str]:
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:QUALIA_ACL_INSPECT_TARGET
foreach ($rule in @($acl.Access)) {
    try {
        $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        $rule.IdentityReference.Value
    }
}
"""
    env = os.environ.copy()
    env["QUALIA_ACL_INSPECT_TARGET"] = str(path)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect Windows ACL for {path}: {result.stderr}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def stat_with_changes(info, **changes):
    values = {
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "st_mode": info.st_mode,
        "st_size": info.st_size,
        "st_atime_ns": info.st_atime_ns,
        "st_mtime_ns": info.st_mtime_ns,
        "st_ctime_ns": info.st_ctime_ns,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import services.local_backup as local_backup

    with tempfile.TemporaryDirectory(prefix="qualia_snapshot_fencing_") as tmp:
        root = Path(tmp)

        generation_source = root / "generation-source.txt"
        generation_target = root / "generation-target.txt"
        generation_source.write_text("original", encoding="utf-8")
        checked = generation_source.lstat()
        same_inode_generation = stat_with_changes(
            checked,
            st_size=checked.st_size + 1,
            st_mtime_ns=checked.st_mtime_ns + 1,
            st_ctime_ns=checked.st_ctime_ns + 1,
        )
        original_fstat = local_backup.os.fstat

        def fake_same_inode_fstat(_fd):
            return same_inode_generation

        local_backup.os.fstat = fake_same_inode_fstat
        generation_rejected = False
        try:
            local_backup._copy_regular_snapshot_file(generation_source, generation_target)
        except ValueError as exc:
            generation_rejected = "changed during snapshot" in str(exc)
        finally:
            local_backup.os.fstat = original_fstat
        failures += check(
            "generation token rejects replacement with the same device/inode identity",
            local_backup._stat_identity(checked)
            == local_backup._stat_identity(same_inode_generation)
            and local_backup._snapshot_stat_token(checked)
            != local_backup._snapshot_stat_token(same_inode_generation)
            and generation_rejected
            and not generation_target.exists(),
            (
                f"identity={local_backup._stat_identity(checked)} "
                f"before={local_backup._snapshot_stat_token(checked)} "
                f"after={local_backup._snapshot_stat_token(same_inode_generation)}"
            ),
        )

        atime_source = root / "atime-source.txt"
        atime_target = root / "atime-target.txt"
        atime_source.write_text("atime-payload", encoding="utf-8")
        atime_fd = os.open(atime_source, os.O_RDONLY | int(getattr(os, "O_BINARY", 0)))
        opened = os.fstat(atime_fd)
        after_read = stat_with_changes(opened, st_atime_ns=opened.st_atime_ns + 10_000_000)
        original_fstat = local_backup.os.fstat
        original_apply_metadata = local_backup._apply_snapshot_metadata
        applied = {"info": None}

        def fake_post_read_fstat(_fd):
            return after_read

        def capture_metadata(_target, info, _attributes=None):
            applied["info"] = info

        local_backup.os.fstat = fake_post_read_fstat
        local_backup._apply_snapshot_metadata = capture_metadata
        try:
            local_backup._copy_open_regular_snapshot_file(
                atime_fd,
                atime_target,
                opened,
                str(atime_source),
                preserve_xattrs=False,
            )
        finally:
            local_backup.os.fstat = original_fstat
            local_backup._apply_snapshot_metadata = original_apply_metadata
            os.close(atime_fd)
        failures += check(
            "rollback metadata keeps the pre-read atime while final stat is validation-only",
            applied["info"] is opened
            and after_read.st_atime_ns != opened.st_atime_ns
            and local_backup._snapshot_stat_token(after_read)
            == local_backup._snapshot_stat_token(opened),
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
                    os.fspath(path) == "payload.txt"
                    and kwargs.get("dir_fd") is not None
                    and not swapped["done"]
                ):
                    inner.rename(held)
                    inner.symlink_to(external, target_is_directory=True)
                    swapped["done"] = True
                return original_open(path, flags, *args, **kwargs)

            local_backup.os.open = replace_ancestor_before_leaf_open
            try:
                local_backup._snapshot_tree_no_links_pinned(source, target)
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
                    os.fspath(path) == "payload.txt"
                    and kwargs.get("dir_fd") is not None
                    and not swapped_collect["done"]
                ):
                    collect_inner.rename(collect_held)
                    collect_inner.symlink_to(collect_external, target_is_directory=True)
                    swapped_collect["done"] = True
                return original_open(path, flags, *args, **kwargs)

            local_backup.os.open = replace_collect_ancestor
            try:
                entries = local_backup._collect_tree_pinned(
                    collect_source,
                    "uploads",
                    collect_stage,
                )
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

            if resource is not None:
                wide_source = root / "wide-source"
                wide_stage = root / "wide-stage"
                wide_snapshot = root / "wide-snapshot"
                wide_source.mkdir()
                for index in range(96):
                    child = wide_source / f"d{index:03d}"
                    child.mkdir()
                    (child / "payload.txt").write_text(str(index), encoding="utf-8")

                original_limits = resource.getrlimit(resource.RLIMIT_NOFILE)
                soft_limit, hard_limit = original_limits
                finite_soft = 32 if soft_limit == resource.RLIM_INFINITY else min(int(soft_limit), 32)
                if finite_soft >= 16:
                    resource.setrlimit(resource.RLIMIT_NOFILE, (finite_soft, hard_limit))
                    descriptor_bounded = False
                    try:
                        wide_entries = local_backup._collect_tree_pinned(
                            wide_source,
                            "uploads",
                            wide_stage,
                        )
                        local_backup._snapshot_tree_no_links_pinned(
                            wide_source,
                            wide_snapshot,
                        )
                        descriptor_bounded = (
                            len(wide_entries) == 96
                            and len(list(wide_snapshot.glob("d*/payload.txt"))) == 96
                        )
                    finally:
                        resource.setrlimit(resource.RLIMIT_NOFILE, original_limits)
                    failures += check(
                        "POSIX pinned traversal keeps descriptors bounded across many siblings",
                        descriptor_bounded,
                        f"soft_limit={finite_soft}",
                    )
                else:
                    failures += check(
                        "POSIX pinned traversal descriptor-bound regression",
                        True,
                        f"SKIP: existing RLIMIT_NOFILE too low ({soft_limit})",
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

        if os.name == "nt":
            acl_dir = root / "acl-boundary"
            acl_dir.mkdir()
            grant = subprocess.run(
                ["icacls", str(acl_dir), "/grant", "*S-1-1-0:(OI)(CI)R"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if grant.returncode != 0:
                raise RuntimeError(f"could not seed explicit Everyone ACL: {grant.stderr}")

            local_backup._restrict_permissions(acl_dir, 0o700)
            inherited_child = acl_dir / "inherited.txt"
            inherited_child.write_text("sensitive", encoding="utf-8")
            expected_sid = local_backup._current_windows_user_sid()
            directory_sids = windows_acl_sids(acl_dir)
            child_sids = windows_acl_sids(inherited_child)
            failures += check(
                "Windows backup ACL removes unrelated explicit grants before child creation",
                directory_sids == {expected_sid} and child_sids == {expected_sid},
                f"directory={sorted(directory_sids)}, child={sorted(child_sids)}",
            )
        else:
            failures += check(
                "Windows explicit-ACE privacy regression",
                True,
                "SKIP: Windows ACL semantics unavailable on this platform",
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
        failures += check(
            "pinned traversal is depth-first rather than accumulating sibling descriptors",
            "def walk(current_fd: int, relative_dir: Path) -> None:" in source_text
            and "open_fds: set[int]" not in source_text
            and "pending: list[tuple[int, Path]]" not in source_text,
        )
        failures += check(
            "Windows privacy implementation constructs a protected current-user-only DACL",
            "SetAccessRuleProtection($true, $false)" in source_text
            and "RemoveAccessRuleSpecific" in source_text
            and "FileSystemAccessRule" in source_text,
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
