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
    storage_paths = root / "services" / "storage_paths.py"
    raw_storage = root / "services" / "raw_snapshot_storage.py"
    smoke = root / "tests" / "smoke_raw_snapshot_storage_fencing.py"

    replace_once(
        storage_paths,
        '''def _windows_create_file_handle(path: Path) -> int:\n''',
        '''def _windows_create_file_handle(path: Path, *, write_through: bool = False) -> int:\n''',
        "windows create signature",
    )
    replace_once(
        storage_paths,
        '''    file_attribute_normal = 0x00000080\n    file_flag_open_reparse_point = 0x00200000\n\n    create_file = _windows_kernel32().CreateFileW\n''',
        '''    file_attribute_normal = 0x00000080\n    file_flag_open_reparse_point = 0x00200000\n    file_flag_write_through = 0x80000000\n\n    create_file = _windows_kernel32().CreateFileW\n''',
        "windows write-through flag",
    )
    replace_once(
        storage_paths,
        '''    handle = create_file(\n        str(path),\n        generic_write | file_read_attributes,\n        file_share_read,\n        None,\n        create_new,\n        file_attribute_normal | file_flag_open_reparse_point,\n        None,\n    )\n''',
        '''    create_flags = file_attribute_normal | file_flag_open_reparse_point\n    if write_through:\n        create_flags |= file_flag_write_through\n\n    handle = create_file(\n        str(path),\n        generic_write | file_read_attributes,\n        file_share_read,\n        None,\n        create_new,\n        create_flags,\n        None,\n    )\n''',
        "windows create flags",
    )
    replace_once(
        storage_paths,
        '''def _create_managed_file_windows(root: Path, relative: Path) -> ManagedWriteFile:\n''',
        '''def _create_managed_file_windows(\n    root: Path,\n    relative: Path,\n    *,\n    write_through: bool = False,\n) -> ManagedWriteFile:\n''',
        "managed windows create signature",
    )
    replace_once(
        storage_paths,
        '''        final_handle = _windows_create_file_handle(display_path)\n''',
        '''        final_handle = _windows_create_file_handle(\n            display_path,\n            write_through=write_through,\n        )\n''',
        "managed windows create call",
    )
    replace_once(
        storage_paths,
        '''def open_managed_file_for_create(root_value: str | Path, stored_path: str) -> ManagedWriteFile:\n''',
        '''def open_managed_file_for_create(\n    root_value: str | Path,\n    stored_path: str,\n    *,\n    write_through: bool = False,\n) -> ManagedWriteFile:\n''',
        "public create signature",
    )
    replace_once(
        storage_paths,
        '''    if os.name == "nt":\n        return _create_managed_file_windows(root, relative)\n    if _supports_pinned_posix_read():\n        return _create_managed_file_posix(root, relative)\n''',
        '''    if os.name == "nt":\n        return _create_managed_file_windows(\n            root,\n            relative,\n            write_through=write_through,\n        )\n    if _supports_pinned_posix_read():\n        return _create_managed_file_posix(root, relative)\n''',
        "public create dispatch",
    )

    replace_once(
        raw_storage,
        '''    _file_generation,\n''',
        '''    _directory_identity,\n    _file_generation,\n''',
        "raw directory identity import",
    )
    replace_once(
        raw_storage,
        '''    is_link_or_reparse,\n    open_managed_file_for_create,\n)\n''',
        '''    is_link_or_reparse,\n    open_managed_file_for_create,\n    open_managed_file_for_read,\n)\n''',
        "raw managed read import",
    )
    marker = '''def write_raw_snapshot_json(\n'''
    helper = '''def _stable_written_identity(info: os.stat_result) -> tuple[int, int, int, int]:\n    return (\n        int(info.st_dev),\n        int(info.st_ino),\n        int(info.st_mode),\n        int(info.st_size),\n    )\n\n\ndef _sync_raw_snapshot_parent_posix(\n    root: Path,\n    filename: str,\n    expected_file_stat: os.stat_result,\n) -> None:\n    opened = _open_raw_dir_posix(root)\n    if opened is None:\n        raise ValueError("raw snapshot directory disappeared before durability sync")\n    root_fd, raw_fd = opened\n    expected_generation = _file_generation(expected_file_stat)\n    try:\n        try:\n            current = os.stat(filename, dir_fd=raw_fd, follow_symlinks=False)\n        except OSError as exc:\n            raise ValueError("raw snapshot disappeared before durability sync") from exc\n        if not stat.S_ISREG(current.st_mode) or _file_generation(current) != expected_generation:\n            raise ValueError("raw snapshot changed before durability sync")\n\n        pinned_raw_identity = _directory_identity(os.fstat(raw_fd))\n        os.fsync(raw_fd)\n        # raw_transcripts itself may have been created for this first snapshot.\n        # Flush the managed root as well so that directory entry is durable.\n        os.fsync(root_fd)\n\n        after_sync = os.stat(filename, dir_fd=raw_fd, follow_symlinks=False)\n        public_raw = os.stat(RAW_SNAPSHOT_DIR, dir_fd=root_fd, follow_symlinks=False)\n        if (\n            _file_generation(after_sync) != expected_generation\n            or not stat.S_ISDIR(public_raw.st_mode)\n            or _directory_identity(public_raw) != pinned_raw_identity\n        ):\n            raise ValueError("raw snapshot namespace changed during durability sync")\n    finally:\n        os.close(raw_fd)\n        os.close(root_fd)\n\n\ndef _verify_written_snapshot_windows(\n    output_dir: str | Path,\n    stored_path: str,\n    expected_file_stat: os.stat_result,\n) -> None:\n    opened = open_managed_file_for_read(output_dir, stored_path)\n    try:\n        if _stable_written_identity(opened.stat_result) != _stable_written_identity(expected_file_stat):\n            raise ValueError("raw snapshot changed after durable write")\n    finally:\n        opened.close()\n\n\n'''
    text = raw_storage.read_text(encoding="utf-8")
    if text.count(marker) != 1:
        raise RuntimeError(f"raw helper insertion: expected one marker, found {text.count(marker)}")
    raw_storage.write_text(text.replace(marker, helper + marker, 1), encoding="utf-8")

    replace_once(
        raw_storage,
        '''    opened = open_managed_file_for_create(output_dir, stored_path)\n    try:\n        opened.stream.write(encoded)\n        opened.stream.flush()\n        os.fsync(opened.stream.fileno())\n    finally:\n        opened.close()\n    return stored_path\n''',
        '''    opened = open_managed_file_for_create(\n        output_dir,\n        stored_path,\n        write_through=True,\n    )\n    final_stat: os.stat_result | None = None\n    try:\n        opened.stream.write(encoded)\n        opened.stream.flush()\n        os.fsync(opened.stream.fileno())\n        final_stat = os.fstat(opened.stream.fileno())\n    finally:\n        opened.close()\n\n    if final_stat is None:\n        raise ValueError("raw snapshot durability state is unavailable")\n    if os.name == "nt":\n        _verify_written_snapshot_windows(output_dir, stored_path, final_stat)\n    else:\n        _sync_raw_snapshot_parent_posix(_root(output_dir), filename, final_stat)\n    return stored_path\n''',
        "raw durable write",
    )

    replace_once(
        smoke,
        '''import os\nimport subprocess\n''',
        '''import os\nimport stat\nimport subprocess\n''',
        "smoke stat import",
    )
    replace_once(
        smoke,
        '''    original_file_generation = storage._file_generation\n''',
        '''    original_file_generation = storage._file_generation\n    original_fsync = storage.os.fsync\n    original_managed_create = storage.open_managed_file_for_create\n''',
        "smoke originals",
    )
    replace_once(
        smoke,
        '''        try:\n            payload = {\n''',
        '''        try:\n            durability_sync_modes: list[str] = []\n            durability_write_through: list[bool] = []\n            durability_root = root / "durability-outputs"\n\n            def recording_fsync(fd):\n                info = os.fstat(fd)\n                durability_sync_modes.append(\n                    "dir" if stat.S_ISDIR(info.st_mode) else "file"\n                )\n                return original_fsync(fd)\n\n            def recording_managed_create(*args, **kwargs):\n                durability_write_through.append(bool(kwargs.get("write_through")))\n                return original_managed_create(*args, **kwargs)\n\n            storage.os.fsync = recording_fsync\n            storage.open_managed_file_for_create = recording_managed_create\n            durability_stored = storage.write_raw_snapshot_json(\n                durability_root,\n                "durability_probe",\n                {"text": "durability-probe"},\n            )\n            storage.os.fsync = original_fsync\n            storage.open_managed_file_for_create = original_managed_create\n            durability_ok = (durability_root / durability_stored).is_file()\n            if os.name == "nt":\n                durability_ok = (\n                    durability_ok\n                    and durability_write_through == [True]\n                    and "file" in durability_sync_modes\n                )\n            else:\n                durability_ok = (\n                    durability_ok\n                    and durability_write_through == [True]\n                    and "file" in durability_sync_modes\n                    and durability_sync_modes.count("dir") >= 2\n                )\n            failures += check(\n                "raw evidence crosses the platform durability barrier before returning",\n                durability_ok,\n                f"write_through={durability_write_through!r} sync_modes={durability_sync_modes!r}",\n            )\n\n            payload = {\n''',
        "smoke durability dynamic",
    )
    replace_once(
        smoke,
        '''            storage._file_generation = original_file_generation\n            config.OUTPUT_DIR = original_output\n''',
        '''            storage._file_generation = original_file_generation\n            storage.os.fsync = original_fsync\n            storage.open_managed_file_for_create = original_managed_create\n            config.OUTPUT_DIR = original_output\n''',
        "smoke cleanup",
    )
    replace_once(
        smoke,
        '''        "open_managed_file_for_create(output_dir, stored_path)" in storage_source\n        and "os.fsync(opened.stream.fileno())" in storage_source,\n''',
        '''        "write_through=True" in storage_source\n        and "os.fsync(opened.stream.fileno())" in storage_source\n        and "os.fsync(raw_fd)" in storage_source\n        and "os.fsync(root_fd)" in storage_source,\n''',
        "smoke source durability contract",
    )


if __name__ == "__main__":
    main()
