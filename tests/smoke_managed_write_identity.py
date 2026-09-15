import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


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

    import config
    import services.file_manager as file_manager
    import services.upload_manager as upload_manager
    from services.storage_paths import open_managed_file_for_create

    original_output = config.OUTPUT_DIR
    original_upload = config.UPLOAD_DIR
    original_db = file_manager.db
    original_begin_registration = file_manager._begin_generated_file_registration
    original_validate_ownership = file_manager._validate_generated_file_ownership

    with tempfile.TemporaryDirectory(prefix="qualia_write_identity_") as tmp:
        root = Path(tmp)
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")
        try:
            # Generated output: write exact object, then replace the project directory
            # with another ordinary directory containing a same-name decoy.
            target = file_manager.prepare_output_target(11, "identity.xlsx")
            opened = file_manager.open_output_target_for_write(target)
            opened.stream.write(b"original-output")
            opened.close()
            original_path = Path(target.full_path)
            original_dir = original_path.parent
            saved_dir = original_dir.with_name(f"{original_dir.name}.original")
            original_dir.rename(saved_dir)
            original_dir.mkdir()
            decoy_path = original_dir / original_path.name
            decoy_path.write_bytes(b"decoy-output")

            rollbacks = []
            file_manager.db = SimpleNamespace(
                session=SimpleNamespace(
                    rollback=lambda: rollbacks.append(True),
                    add=lambda _value: None,
                    commit=lambda: None,
                )
            )
            # This regression isolates managed-file generation identity. The
            # production ownership/session boundary is exercised separately by
            # smoke_generated_file_project_ownership.py and
            # smoke_generated_file_ownership_serialization.py.
            file_manager._begin_generated_file_registration = (
                lambda *, existing_write_reservation: None
            )
            file_manager._validate_generated_file_ownership = (
                lambda _target, *, project_id, interview_id: (
                    int(project_id),
                    int(interview_id) if interview_id is not None else None,
                )
            )
            rejected = False
            try:
                file_manager.register_generated_file(
                    target,
                    project_id=11,
                    file_type="analysis",
                    file_format="xlsx",
                )
            except ValueError as exc:
                rejected = "changed after write" in str(exc)

            failures += check(
                "generated-file registration rejects post-write ordinary-directory replacement",
                rejected and rollbacks and decoy_path.read_bytes() == b"decoy-output",
                f"rejected={rejected} rollbacks={len(rollbacks)}",
            )
            failures += check(
                "generated-file rollback cleanup does not unlink replacement decoy",
                decoy_path.is_file()
                and (saved_dir / original_path.name).read_bytes() == b"original-output",
            )

            # Restore the original pathname. Cleanup may now remove only the exact
            # generation captured by OutputWriteFile.close().
            decoy_path.unlink()
            original_dir.rmdir()
            saved_dir.rename(original_dir)
            file_manager._discard_output_target_if_same_generation(target)
            failures += check(
                "generated-file cleanup removes the exact recorded generation",
                not original_path.exists(),
            )

            # Upload rollback follows the same rule using the write handle's final
            # fstat. A replaced ordinary directory must not redirect cleanup.
            media_target = upload_manager.prepare_media_upload_target(12, "source.wav")
            media_opened = open_managed_file_for_create(
                config.UPLOAD_DIR,
                media_target.stored_path,
            )
            media_opened.stream.write(b"original-upload")
            media_opened.stream.flush()
            media_stat = os.fstat(media_opened.stream.fileno())
            media_opened.close()

            media_path = Path(media_target.full_path)
            media_dir = media_path.parent
            media_saved_dir = media_dir.with_name(f"{media_dir.name}.original")
            media_dir.rename(media_saved_dir)
            media_dir.mkdir()
            media_decoy = media_dir / media_path.name
            media_decoy.write_bytes(b"decoy-upload")

            upload_manager._discard_target_if_same_generation(media_target, media_stat)
            failures += check(
                "upload rollback cleanup does not unlink replacement decoy",
                media_decoy.read_bytes() == b"decoy-upload"
                and (media_saved_dir / media_path.name).read_bytes() == b"original-upload",
            )

            media_decoy.unlink()
            media_dir.rmdir()
            media_saved_dir.rename(media_dir)
            upload_manager._discard_target_if_same_generation(media_target, media_stat)
            failures += check(
                "upload rollback cleanup removes the exact recorded generation",
                not media_path.exists(),
            )
        finally:
            file_manager._begin_generated_file_registration = original_begin_registration
            file_manager._validate_generated_file_ownership = original_validate_ownership
            file_manager.db = original_db
            config.OUTPUT_DIR = original_output
            config.UPLOAD_DIR = original_upload

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
