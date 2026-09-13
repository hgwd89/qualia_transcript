import os
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import services.local_backup as local_backup

    with tempfile.TemporaryDirectory(prefix="qualia_backup_collect_guard_") as tmp:
        root = Path(tmp)

        normal_source = root / "normal-source"
        normal_stage = root / "normal-stage"
        normal_source.mkdir()
        (normal_source / "regular.txt").write_text("regular payload", encoding="utf-8")
        entries = local_backup._collect_tree(normal_source, "uploads", normal_stage)
        staged_regular = normal_stage / "uploads" / "regular.txt"
        failures += check(
            "backup collector copies regular files and records manifest metadata",
            len(entries) == 1
            and entries[0].get("path") == "uploads/regular.txt"
            and staged_regular.read_text(encoding="utf-8") == "regular payload",
        )

        race_source = root / "race-source"
        race_stage = root / "race-stage"
        race_source.mkdir()
        race_file = race_source / "collector-race.txt"
        retained_original = race_source / "race-original-retained.txt"
        race_file.write_text("original", encoding="utf-8")
        original_os_open = local_backup.os.open
        replaced = {"done": False}

        def replace_before_descriptor_open(path, flags, *args, **kwargs):
            if Path(path).name == "collector-race.txt" and not replaced["done"]:
                # Keep the original file allocated so the replacement cannot
                # accidentally reuse the same inode/file-id immediately.
                race_file.rename(retained_original)
                race_file.write_text("replacement", encoding="utf-8")
                replaced["done"] = True
            return original_os_open(path, flags, *args, **kwargs)

        local_backup.os.open = replace_before_descriptor_open
        race_rejected = False
        try:
            local_backup._collect_tree(race_source, "uploads", race_stage)
        except ValueError as exc:
            race_rejected = "changed during snapshot" in str(exc)
        finally:
            local_backup.os.open = original_os_open
        failures += check(
            "backup collector rejects pathname replacement before descriptor copy",
            replaced["done"]
            and race_rejected
            and retained_original.read_text(encoding="utf-8") == "original"
            and not (race_stage / "uploads" / "collector-race.txt").exists(),
        )

        linked_source = root / "linked-source"
        linked_stage = root / "linked-stage"
        linked_source.mkdir()
        linked_file = linked_source / "linked.txt"
        linked_file.write_text("must not stage", encoding="utf-8")
        original_link_check = local_backup.is_link_or_reparse
        local_backup.is_link_or_reparse = lambda path: Path(path).name == "linked.txt"
        linked_rejected = False
        try:
            local_backup._collect_tree(linked_source, "uploads", linked_stage)
        except ValueError as exc:
            linked_rejected = "linked/reparse" in str(exc)
        finally:
            local_backup.is_link_or_reparse = original_link_check
        failures += check(
            "backup collector rejects linked/reparse entries",
            linked_rejected and not (linked_stage / "uploads" / "linked.txt").exists(),
        )

        pending_source = root / "pending-source"
        pending_stage = root / "pending-stage"
        pending_source.mkdir()
        pending_dir = pending_source / "nested"
        pending_dir.mkdir()
        link_checks = {"nested": 0}

        def becomes_linked_on_recheck(path):
            if Path(path).name != "nested":
                return False
            link_checks["nested"] += 1
            return link_checks["nested"] >= 2

        local_backup.is_link_or_reparse = becomes_linked_on_recheck
        pending_rejected = False
        try:
            local_backup._collect_tree(pending_source, "uploads", pending_stage)
        except ValueError as exc:
            pending_rejected = "directory changed during collection" in str(exc)
        finally:
            local_backup.is_link_or_reparse = original_link_check
        failures += check(
            "backup collector revalidates pending directories before traversal",
            link_checks["nested"] >= 2 and pending_rejected,
        )

        source_text = (repo_root / "services" / "local_backup.py").read_text(encoding="utf-8")
        failures += check(
            "backup collector uses descriptor-fenced copy and rejects unsupported entries",
            "_copy_regular_snapshot_file(source, staged)" in source_text
            and "managed backup tree contains unsupported entry type" in source_text
            and "managed backup directory changed during collection" in source_text
            and "shutil.copy2(source, staged)" not in source_text,
        )

        if os.name != "nt" and hasattr(os, "mkfifo"):
            special_source = root / "special-source"
            special_stage = root / "special-stage"
            special_source.mkdir()
            os.mkfifo(special_source / "unsupported.fifo")
            special_rejected = False
            try:
                local_backup._collect_tree(special_source, "uploads", special_stage)
            except ValueError as exc:
                special_rejected = "unsupported entry type" in str(exc)
            failures += check(
                "backup collector rejects unsupported special entries",
                special_rejected,
            )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
