import json
import os
import stat
import subprocess
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


def make_directory_link(link: Path, target: Path) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and link.exists()
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def remove_directory_link(link: Path) -> None:
    if not link.exists() and not link.is_symlink():
        return
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    import services.raw_snapshot_storage as storage
    import services.transcription as transcription

    original_output = config.OUTPUT_DIR
    original_listdir = storage.os.listdir
    original_windows_open = storage._windows_open_path_handle
    original_file_generation = storage._file_generation
    original_fsync = storage.os.fsync
    original_managed_create = storage.open_managed_file_for_create

    with tempfile.TemporaryDirectory(prefix="qualia_raw_snapshot_fencing_") as tmp:
        root = Path(tmp)
        evidence_root = root / "outputs"
        config.OUTPUT_DIR = str(evidence_root)
        try:
            durability_sync_modes: list[str] = []
            durability_write_through: list[bool] = []
            durability_root = root / "durability-outputs"

            def recording_fsync(fd):
                info = os.fstat(fd)
                durability_sync_modes.append(
                    "dir" if stat.S_ISDIR(info.st_mode) else "file"
                )
                return original_fsync(fd)

            def recording_managed_create(*args, **kwargs):
                durability_write_through.append(bool(kwargs.get("write_through")))
                return original_managed_create(*args, **kwargs)

            storage.os.fsync = recording_fsync
            storage.open_managed_file_for_create = recording_managed_create
            durability_stored = storage.write_raw_snapshot_json(
                durability_root,
                "durability_probe",
                {"text": "durability-probe"},
            )
            storage.os.fsync = original_fsync
            storage.open_managed_file_for_create = original_managed_create
            durability_ok = (durability_root / durability_stored).is_file()
            if os.name == "nt":
                durability_ok = (
                    durability_ok
                    and durability_write_through == [True]
                    and "file" in durability_sync_modes
                )
            else:
                durability_ok = (
                    durability_ok
                    and durability_write_through == [True]
                    and "file" in durability_sync_modes
                    and durability_sync_modes.count("dir") >= 2
                )
            failures += check(
                "raw evidence crosses the platform durability barrier before returning",
                durability_ok,
                f"write_through={durability_write_through!r} sync_modes={durability_sync_modes!r}",
            )

            payload = {
                "transcription_id": 1,
                "interview_id": 2,
                "created_at_utc": "2026-09-13T00:05:00+00:00",
                "sha256": "placeholder",
                "text": "immutable evidence",
            }
            stored = storage.write_raw_snapshot_json(
                evidence_root,
                "transcription_1_source",
                payload,
            )
            stored_path = evidence_root / stored
            batch = storage.read_raw_snapshot_batch(evidence_root)
            failures += check(
                "raw snapshot write/read stays under canonical evidence directory",
                stored.startswith("raw_transcripts/")
                and stored_path.is_file()
                and len(batch) == 1
                and batch[0][0] == stored_path.name
                and json.loads(batch[0][1].decode("utf-8"))["text"] == "immutable evidence",
                f"stored={stored} batch_names={[name for name, _ in batch]}",
            )

            second = storage.write_raw_snapshot_json(
                evidence_root,
                "transcription_1_source",
                payload,
            )
            failures += check(
                "same raw snapshot basename remains immutable and collision-free",
                second != stored
                and (evidence_root / second).is_file()
                and stored_path.read_bytes() == batch[0][1],
                f"first={stored} second={second}",
            )

            raw_snapshot_path, digest = transcription._write_raw_transcript_snapshot(
                10,
                20,
                "test-model",
                "ja",
                "provider exact text",
                snapshot_tag="provider",
            )
            manifest_path = transcription._write_chunk_manifest(
                10,
                20,
                "test-model",
                "ja",
                300.0,
                0.0,
                [{"index": 0, "text": "provider exact text"}],
                "complete",
            )
            transcription_batch = dict(storage.read_raw_snapshot_batch(evidence_root))
            failures += check(
                "transcription snapshot and manifest use managed evidence storage",
                Path(raw_snapshot_path).parts[0] == "raw_transcripts"
                and Path(manifest_path).parts[0] == "raw_transcripts"
                and Path(raw_snapshot_path).name in transcription_batch
                and Path(manifest_path).name in transcription_batch
                and digest,
                f"snapshot={raw_snapshot_path} manifest={manifest_path}",
            )

            linked_root = root / "linked-root"
            outside = root / "outside"
            linked_root.mkdir()
            outside.mkdir()
            linked_raw = linked_root / "raw_transcripts"
            link_ready = make_directory_link(linked_raw, outside)
            failures += check(
                "linked raw snapshot fixture is available",
                link_ready,
            )
            if link_ready:
                try:
                    failures += check(
                        "raw snapshot writer rejects linked/reparse evidence directory",
                        raises_value_error(
                            lambda: storage.write_raw_snapshot_json(
                                linked_root,
                                "transcription_30",
                                payload,
                            )
                        ),
                    )
                    failures += check(
                        "raw snapshot batch reader rejects linked/reparse evidence directory",
                        raises_value_error(
                            lambda: storage.read_raw_snapshot_batch(linked_root)
                        ),
                    )
                finally:
                    remove_directory_link(linked_raw)

            race_root = root / "race-root"
            race_stored = storage.write_raw_snapshot_json(
                race_root,
                "transcription_40_source",
                {
                    "transcription_id": 40,
                    "interview_id": 41,
                    "created_at_utc": "2026-09-13T01:00:00+00:00",
                    "text": "pinned-original",
                },
            )
            race_raw = race_root / "raw_transcripts"
            race_name = Path(race_stored).name
            race_original_bytes = (race_raw / race_name).read_bytes()
            saved_raw = race_root / "raw_transcripts.pinned-original"
            attempted = False
            swapped = False
            blocked = False

            def racing_listdir(path):
                nonlocal attempted, swapped
                if not attempted and isinstance(path, int):
                    attempted = True
                    race_raw.rename(saved_raw)
                    race_raw.mkdir()
                    (race_raw / race_name).write_bytes(b"replacement-decoy")
                    swapped = True
                return original_listdir(path)

            def racing_windows_open(path, *args, **kwargs):
                nonlocal attempted, blocked, swapped
                if not attempted and kwargs.get("directory") is False:
                    attempted = True
                    try:
                        race_raw.rename(saved_raw)
                        race_raw.mkdir()
                        (race_raw / race_name).write_bytes(b"replacement-decoy")
                        swapped = True
                    except OSError:
                        blocked = True
                return original_windows_open(path, *args, **kwargs)

            if os.name == "nt":
                storage._windows_open_path_handle = racing_windows_open
            else:
                storage.os.listdir = racing_listdir
            rejected = False
            raced_bytes = None
            try:
                try:
                    raced_batch = dict(storage.read_raw_snapshot_batch(race_root))
                    raced_bytes = raced_batch.get(race_name)
                except ValueError:
                    rejected = True

                replacement_bytes = None
                if swapped and race_raw.exists() and (race_raw / race_name).exists():
                    replacement_bytes = (race_raw / race_name).read_bytes()

                if os.name == "nt":
                    ok = (
                        attempted
                        and (
                            (blocked and not swapped and not rejected and raced_bytes == race_original_bytes)
                            or (
                                swapped
                                and rejected
                                and raced_bytes is None
                                and replacement_bytes == b"replacement-decoy"
                            )
                        )
                    )
                else:
                    ok = (
                        attempted
                        and swapped
                        and not rejected
                        and raced_bytes == race_original_bytes
                        and replacement_bytes == b"replacement-decoy"
                    )

                failures += check(
                    "raw snapshot batch read never consumes a replacement evidence namespace",
                    ok,
                    (
                        f"attempted={attempted} swapped={swapped} blocked={blocked} "
                        f"rejected={rejected} raced={raced_bytes!r} replacement={replacement_bytes!r}"
                    ),
                )
            finally:
                storage.os.listdir = original_listdir
                storage._windows_open_path_handle = original_windows_open
                if swapped:
                    replacement = race_raw / race_name
                    if replacement.exists():
                        replacement.unlink()
                    if race_raw.exists():
                        race_raw.rmdir()
                    saved_raw.rename(race_raw)

            if os.name != "nt":
                mutation_root = root / "mutation-root"
                mutation_stored = storage.write_raw_snapshot_json(
                    mutation_root,
                    "transcription_50_source",
                    {
                        "transcription_id": 50,
                        "interview_id": 51,
                        "created_at_utc": "2026-09-13T02:00:00+00:00",
                        "text": "stable-before-read",
                    },
                )
                mutation_path = mutation_root / mutation_stored
                generation_calls = 0
                mutation_ran = False

                def mutating_generation(info):
                    nonlocal generation_calls, mutation_ran
                    generation = original_file_generation(info)
                    generation_calls += 1
                    if generation_calls == 3 and not mutation_ran:
                        with mutation_path.open("ab") as stream:
                            stream.write(b"\nconcurrent-tamper")
                            stream.flush()
                            os.fsync(stream.fileno())
                        mutation_ran = True
                    return generation

                storage._file_generation = mutating_generation
                try:
                    mutation_rejected = raises_value_error(
                        lambda: storage.read_raw_snapshot_batch(mutation_root)
                    )
                finally:
                    storage._file_generation = original_file_generation
                failures += check(
                    "POSIX raw snapshot batch rejects in-place mutation before read completion",
                    mutation_ran and mutation_rejected,
                    f"mutation_ran={mutation_ran} rejected={mutation_rejected} calls={generation_calls}",
                )

        finally:
            storage.os.listdir = original_listdir
            storage._windows_open_path_handle = original_windows_open
            storage._file_generation = original_file_generation
            storage.os.fsync = original_fsync
            storage.open_managed_file_for_create = original_managed_create
            config.OUTPUT_DIR = original_output

    storage_source = (repo_root / "services" / "raw_snapshot_storage.py").read_text(encoding="utf-8")
    transcription_source = (repo_root / "services" / "transcription.py").read_text(encoding="utf-8")
    provenance_source = (repo_root / "services" / "raw_snapshot_provenance.py").read_text(encoding="utf-8")
    readiness_source = (repo_root / "services" / "readiness_validation.py").read_text(encoding="utf-8")

    failures += check(
        "raw evidence writes use exclusive managed create plus durability flush",
        "write_through=True" in storage_source
        and "os.fsync(opened.stream.fileno())" in storage_source
        and "os.fsync(raw_fd)" in storage_source
        and "os.fsync(root_fd)" in storage_source,
    )
    failures += check(
        "raw evidence batch reader pins or identity-revalidates the directory namespace",
        "os.listdir(raw_fd)" in storage_source
        and "_pin_raw_dir_windows" in storage_source
        and "_assert_windows_raw_path_identity" in storage_source
        and "_windows_handle_identity" in storage_source,
    )
    failures += check(
        "POSIX raw evidence reads revalidate generation after bytes are consumed",
        "after_read = os.fstat(stream.fileno())" in storage_source
        and "raw snapshot changed during read" in storage_source,
    )
    failures += check(
        "transcription no longer opens raw evidence pathnames directly",
        "write_raw_snapshot_json(config.OUTPUT_DIR, base_name, payload)" in transcription_source
        and 'open(abs_path, "x"' not in transcription_source
        and "os.makedirs(raw_dir" not in transcription_source,
    )
    failures += check(
        "provenance and readiness consume one pinned raw evidence batch",
        "read_raw_snapshot_batch(output_dir)" in provenance_source
        and "read_raw_snapshot_batch(output_dir)" in readiness_source
        and '.glob("*.json")' not in provenance_source
        and '.glob("*.json")' not in readiness_source,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
