from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    storage = root / "services" / "raw_snapshot_storage.py"
    smoke = root / "tests" / "smoke_raw_snapshot_storage_fencing.py"
    architecture = root / "docs" / "architecture.md"
    testing = root / "docs" / "testing.md"

    replace_once(
        storage,
        '''import json\nimport os\nimport stat\n''',
        '''import hashlib\nimport json\nimport os\nimport stat\n''',
        "hashlib import",
    )

    replace_once(
        storage,
        '''def _sync_raw_snapshot_parent_posix(\n    root: Path,\n    filename: str,\n    expected_file_stat: os.stat_result,\n) -> None:\n''',
        '''def _sync_raw_snapshot_parent_posix(\n    root: Path,\n    filename: str,\n    expected_file_stat: os.stat_result,\n    *,\n    sync_root_parent: bool,\n) -> None:\n''',
        "posix durability signature",
    )

    replace_once(
        storage,
        '''        if (\n            _file_generation(after_sync) != expected_generation\n            or not stat.S_ISDIR(public_raw.st_mode)\n            or _directory_identity(public_raw) != pinned_raw_identity\n        ):\n            raise ValueError("raw snapshot namespace changed during durability sync")\n    finally:\n        os.close(raw_fd)\n        os.close(root_fd)\n''',
        '''        if (\n            _file_generation(after_sync) != expected_generation\n            or not stat.S_ISDIR(public_raw.st_mode)\n            or _directory_identity(public_raw) != pinned_raw_identity\n        ):\n            raise ValueError("raw snapshot namespace changed during durability sync")\n\n        if sync_root_parent and root.parent != root:\n            parent_fd = _open_posix_directory_chain(root.parent)\n            try:\n                pinned_root_identity = _directory_identity(os.fstat(root_fd))\n                try:\n                    before_root = os.stat(\n                        root.name,\n                        dir_fd=parent_fd,\n                        follow_symlinks=False,\n                    )\n                except OSError as exc:\n                    raise ValueError(\n                        "raw snapshot output root disappeared before parent durability sync"\n                    ) from exc\n                if (\n                    not stat.S_ISDIR(before_root.st_mode)\n                    or _directory_identity(before_root) != pinned_root_identity\n                ):\n                    raise ValueError(\n                        "raw snapshot output root changed before parent durability sync"\n                    )\n                os.fsync(parent_fd)\n                after_root = os.stat(\n                    root.name,\n                    dir_fd=parent_fd,\n                    follow_symlinks=False,\n                )\n                if _directory_identity(after_root) != pinned_root_identity:\n                    raise ValueError(\n                        "raw snapshot output root changed during parent durability sync"\n                    )\n            finally:\n                os.close(parent_fd)\n    finally:\n        os.close(raw_fd)\n        os.close(root_fd)\n''',
        "posix root-parent fsync",
    )

    replace_once(
        storage,
        '''def _verify_written_snapshot_windows(\n    output_dir: str | Path,\n    stored_path: str,\n    expected_file_stat: os.stat_result,\n) -> None:\n    opened = open_managed_file_for_read(output_dir, stored_path)\n    try:\n        if _stable_written_identity(opened.stat_result) != _stable_written_identity(expected_file_stat):\n            raise ValueError("raw snapshot changed after durable write")\n    finally:\n        opened.close()\n''',
        '''def _verify_written_snapshot_windows(\n    output_dir: str | Path,\n    stored_path: str,\n    expected_file_stat: os.stat_result,\n    expected_sha256: bytes,\n) -> None:\n    opened = open_managed_file_for_read(output_dir, stored_path)\n    try:\n        expected_identity = _stable_written_identity(expected_file_stat)\n        if _stable_written_identity(opened.stat_result) != expected_identity:\n            raise ValueError("raw snapshot changed after durable write")\n\n        digest = hashlib.sha256()\n        for chunk in iter(lambda: opened.stream.read(1024 * 1024), b""):\n            digest.update(chunk)\n        after_read = os.fstat(opened.stream.fileno())\n        if _stable_written_identity(after_read) != expected_identity:\n            raise ValueError("raw snapshot changed during durable verification")\n        if digest.digest() != expected_sha256:\n            raise ValueError("raw snapshot bytes changed after durable write")\n    finally:\n        opened.close()\n''',
        "windows byte verification",
    )

    replace_once(
        storage,
        '''    if not isinstance(payload, dict):\n        raise ValueError("raw snapshot payload must be a JSON object")\n    ensure_raw_snapshot_dir(output_dir)\n    base = _validate_base_name(base_name)\n''',
        '''    if not isinstance(payload, dict):\n        raise ValueError("raw snapshot payload must be a JSON object")\n    root = _root(output_dir)\n    root_existed = root.exists()\n    ensure_raw_snapshot_dir(output_dir)\n    base = _validate_base_name(base_name)\n''',
        "capture output-root creation",
    )

    replace_once(
        storage,
        '''    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")\n\n    opened = open_managed_file_for_create(\n''',
        '''    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")\n    expected_sha256 = hashlib.sha256(encoded).digest()\n\n    opened = open_managed_file_for_create(\n''',
        "expected byte digest",
    )

    replace_once(
        storage,
        '''    if final_stat is None:\n        raise ValueError("raw snapshot durability state is unavailable")\n    if os.name == "nt":\n        _verify_written_snapshot_windows(output_dir, stored_path, final_stat)\n    else:\n        _sync_raw_snapshot_parent_posix(_root(output_dir), filename, final_stat)\n    return stored_path\n''',
        '''    if final_stat is None:\n        raise ValueError("raw snapshot durability state is unavailable")\n    if os.name == "nt":\n        _verify_written_snapshot_windows(\n            output_dir,\n            stored_path,\n            final_stat,\n            expected_sha256,\n        )\n    else:\n        _sync_raw_snapshot_parent_posix(\n            root,\n            filename,\n            final_stat,\n            sync_root_parent=not root_existed,\n        )\n    return stored_path\n''',
        "durability dispatch",
    )

    replace_once(
        smoke,
        '''    original_fsync = storage.os.fsync\n    original_managed_create = storage.open_managed_file_for_create\n''',
        '''    original_fsync = storage.os.fsync\n    original_managed_create = storage.open_managed_file_for_create\n    original_managed_read = storage.open_managed_file_for_read\n''',
        "smoke originals",
    )

    replace_once(
        smoke,
        '''                    and "file" in durability_sync_modes\n                    and durability_sync_modes.count("dir") >= 2\n''',
        '''                    and "file" in durability_sync_modes\n                    and durability_sync_modes.count("dir") >= 3\n''',
        "POSIX parent-dir durability assertion",
    )

    replace_once(
        smoke,
        '''            failures += check(\n                "raw evidence crosses the platform durability barrier before returning",\n                durability_ok,\n                f"write_through={durability_write_through!r} sync_modes={durability_sync_modes!r}",\n            )\n\n            payload = {\n''',
        '''            failures += check(\n                "raw evidence crosses the platform durability barrier before returning",\n                durability_ok,\n                f"write_through={durability_write_through!r} sync_modes={durability_sync_modes!r}",\n            )\n\n            if os.name == "nt":\n                tamper_root = root / "windows-byte-tamper"\n                tamper_attempted = False\n\n                def tampering_managed_read(output_dir, stored_path):\n                    nonlocal tamper_attempted\n                    if not tamper_attempted:\n                        tamper_attempted = True\n                        path = Path(output_dir) / stored_path\n                        original_bytes = path.read_bytes()\n                        path.write_bytes(b"X" * len(original_bytes))\n                    return original_managed_read(output_dir, stored_path)\n\n                storage.open_managed_file_for_read = tampering_managed_read\n                try:\n                    same_length_tamper_rejected = raises_value_error(\n                        lambda: storage.write_raw_snapshot_json(\n                            tamper_root,\n                            "same_length_tamper",\n                            {"text": "must-remain-exact"},\n                        )\n                    )\n                finally:\n                    storage.open_managed_file_for_read = original_managed_read\n                failures += check(\n                    "Windows durable reopen rejects same-length byte replacement",\n                    tamper_attempted and same_length_tamper_rejected,\n                    f"attempted={tamper_attempted} rejected={same_length_tamper_rejected}",\n                )\n\n            payload = {\n''',
        "Windows same-length tamper regression",
    )

    replace_once(
        smoke,
        '''            storage.os.fsync = original_fsync\n            storage.open_managed_file_for_create = original_managed_create\n            config.OUTPUT_DIR = original_output\n''',
        '''            storage.os.fsync = original_fsync\n            storage.open_managed_file_for_create = original_managed_create\n            storage.open_managed_file_for_read = original_managed_read\n            config.OUTPUT_DIR = original_output\n''',
        "smoke cleanup",
    )

    replace_once(
        smoke,
        '''        and "os.fsync(raw_fd)" in storage_source\n        and "os.fsync(root_fd)" in storage_source,\n''',
        '''        and "os.fsync(raw_fd)" in storage_source\n        and "os.fsync(root_fd)" in storage_source\n        and "os.fsync(parent_fd)" in storage_source\n        and "hashlib.sha256" in storage_source\n        and "raw snapshot bytes changed after durable write" in storage_source,\n''',
        "source durability contract",
    )

    replace_once(
        architecture,
        '''Derived data may reference raw data by IDs and source quotes, but must not overwrite raw data. Any code path that changes `Segment.text` must be treated as high risk and requires explicit approval.\n\n## AI Analysis Review Contract\n''',
        '''Derived data may reference raw data by IDs and source quotes, but must not overwrite raw data. Any code path that changes `Segment.text` must be treated as high risk and requires explicit approval.\n\n## Raw Transcript Evidence Durability Contract\n\nRaw transcript JSON snapshots are immutable source evidence. `services/raw_snapshot_storage.py` must not report a successful write until both the bytes and the filesystem namespace needed to find those bytes have crossed the platform durability barrier. Canonical transcription success is committed only after this evidence-write boundary returns.\n\nOn POSIX, evidence creation uses ancestry-pinned exclusive create, flushes and `fsync`s the file, then pins and revalidates the `raw_transcripts` directory while `fsync`ing that directory and the managed output root. When the output root itself was created by the write, its parent directory is also identity-checked and `fsync`ed so the new root entry is durable. Namespace or file-generation replacement during this barrier fails closed.\n\nOn Windows, raw evidence is created with `CREATE_NEW` plus `FILE_FLAG_WRITE_THROUGH`, followed by the normal file flush. After close, the file is reopened through the ancestry-pinned managed-read boundary and its exact bytes are SHA-256 verified against the serialized payload; same-size in-place replacement therefore cannot pass merely by preserving file identity/length metadata.\n\n## AI Analysis Review Contract\n''',
        "architecture durability contract",
    )

    replace_once(
        architecture,
        '''- `services/raw_snapshot_storage.py`: ancestry-pinned exclusive creation and batch reads for immutable raw transcript JSON evidence.\n''',
        '''- `services/raw_snapshot_storage.py`: ancestry-pinned exclusive creation, crash-durable publication, byte verification, and batch reads for immutable raw transcript JSON evidence.\n''',
        "architecture service description",
    )

    replace_once(
        testing,
        '''- managed-storage path guards that reject symlinks and Windows junction/reparse entries below configured output/upload roots; both reads and new-file writes use ancestry-pinned acquisition rather than check-then-reopen pathnames. POSIX descends through descriptor-relative `open/stat` plus `O_NOFOLLOW` and creates with `O_CREAT|O_EXCL`; Windows retains read-share-only component handles opened with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse handles, denies both write and delete sharing until final acquisition, and creates destinations with `CREATE_NEW`\n- backup/restore integrity hardening:''',
        '''- managed-storage path guards that reject symlinks and Windows junction/reparse entries below configured output/upload roots; both reads and new-file writes use ancestry-pinned acquisition rather than check-then-reopen pathnames. POSIX descends through descriptor-relative `open/stat` plus `O_NOFOLLOW` and creates with `O_CREAT|O_EXCL`; Windows retains read-share-only component handles opened with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse handles, denies both write and delete sharing until final acquisition, and creates destinations with `CREATE_NEW`\n- raw transcript evidence crash durability: immutable JSON evidence must cross the platform durability barrier before transcription can commit canonical success. POSIX verifies the created file generation and pinned namespace while fsyncing the file, `raw_transcripts`, the managed output root, and the output root's parent when that root was newly created. Windows creates raw evidence with `FILE_FLAG_WRITE_THROUGH`, closes it, reopens it through the pinned managed-read boundary, and verifies SHA-256 of the retained bytes so same-length replacement is rejected\n- backup/restore integrity hardening:''',
        "testing safe-gate bullet",
    )

    replace_once(
        testing,
        '''The managed write commit-window regression uses SQLAlchemy's `before_commit` hook to race the public managed directory only after the exact written generation has already been re-pinned. On POSIX, where renaming an open directory is allowed, it proves both generated outputs and uploads detect the post-guard namespace replacement after commit, compensate the just-created database row, delete only the pinned predecessor generation, and leave the replacement decoy untouched. On Windows, it proves the retained ancestor/final handles deny delete sharing so the same rename is rejected while the DB commit proceeds normally.\n\nThe media-upload/transcription regression also locks the source contract to the implementation:''',
        '''The managed write commit-window regression uses SQLAlchemy's `before_commit` hook to race the public managed directory only after the exact written generation has already been re-pinned. On POSIX, where renaming an open directory is allowed, it proves both generated outputs and uploads detect the post-guard namespace replacement after commit, compensate the just-created database row, delete only the pinned predecessor generation, and leave the replacement decoy untouched. On Windows, it proves the retained ancestor/final handles deny delete sharing so the same rename is rejected while the DB commit proceeds normally.\n\nThe raw-snapshot storage fencing regression also tests durability rather than only pathname safety. A fresh POSIX fixture must observe one file sync plus directory syncs for `raw_transcripts`, the new output root, and its parent before the writer returns. On Windows the fixture records write-through creation and deliberately replaces the just-written JSON with different bytes of exactly the same length between close and the managed verification reopen; the write must fail because the SHA-256 readback no longer matches the serialized payload.\n\nThe media-upload/transcription regression also locks the source contract to the implementation:''',
        "testing durability explanation",
    )


if __name__ == "__main__":
    main()
