from pathlib import Path


ARCH = Path("docs/architecture.md")
TESTING = Path("docs/testing.md")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    arch = ARCH.read_text(encoding="utf-8")
    read_para = (
        "Transcription uses the same ancestry-pinned read boundary for uploaded media, but pathname-only decoders and providers are never handed the mutable managed upload path. `create_media_read_snapshot()` copies the exact already-opened media object into an owner-private temporary file outside `UPLOAD_DIR`, verifies source generation and byte size across the copy, and `services/transcription.py` passes only that private snapshot pathname to PyAV, OpenAI, and faster-whisper. Both transcription provider paths close the snapshot in function-level `finally` blocks, so success and failure remove the temporary copy."
    )
    write_para = (
        "Managed writes use the corresponding ancestry-pinned exclusive-create boundary. `open_managed_file_for_create()` creates a UUID-named destination only once and returns the already-opened binary stream; media upload and every generated-report writer write to that stream instead of reopening `target.full_path`. On POSIX the final file is created descriptor-relative beneath the pinned parent with `O_CREAT|O_EXCL|O_NOFOLLOW`; on Windows ancestor handles deny write/delete sharing while the final file is opened with `CREATE_NEW` and `FILE_FLAG_OPEN_REPARSE_POINT`. An ID-directory pathname replaced after acquisition therefore cannot redirect the write into another tree, and an existing destination is never truncated or followed. Media registration also compares the written handle generation with the managed pathname before committing its row."
    )
    arch = replace_once(
        arch,
        read_para,
        read_para + "\n\n" + write_para,
        "architecture managed-write paragraph",
    )
    ARCH.write_text(arch, encoding="utf-8")

    testing = TESTING.read_text(encoding="utf-8")
    old_generated = (
        "- generated-file integrity, including project-scoped path enforcement, UUID-isolated internal storage for same-name outputs, rollback isolation, bounded storage basenames for long display filenames, rejection of linked/reparse project storage paths, handle-backed download bytes, `Content-Length`/`Last-Modified`/ETag preservation, byte-range `206` behavior, and legacy nullable download-name fallback"
    )
    new_generated = (
        "- generated-file integrity, including project-scoped path enforcement, UUID-isolated internal storage for same-name outputs, rollback isolation, bounded storage basenames for long display filenames, rejection of linked/reparse project storage paths, ancestry-pinned exclusive creation for every DOCX/XLSX/CSV report writer, handle-backed download bytes, `Content-Length`/`Last-Modified`/ETag preservation, byte-range `206` behavior, and legacy nullable download-name fallback"
    )
    testing = replace_once(testing, old_generated, new_generated, "testing generated bullet")

    old_media = (
        "- media-upload integrity, including rejection of linked/reparse interview storage paths and transcription input snapshotting from an ancestry-pinned managed handle; the required regression verifies exact uploaded bytes, a private snapshot pathname outside `UPLOAD_DIR`, stability after the managed ID pathname is replaced, and deterministic snapshot cleanup"
    )
    new_media = (
        "- media-upload integrity, including rejection of linked/reparse interview storage paths, ancestry-pinned exclusive creation of uploaded bytes, post-write generation verification before DB registration, and transcription input snapshotting from an ancestry-pinned managed handle; the required regressions verify exact uploaded bytes, a private snapshot pathname outside `UPLOAD_DIR`, stability after the managed ID pathname is replaced, deterministic snapshot cleanup, and source-level exclusion of pathname-based `FileStorage.save()` writes"
    )
    testing = replace_once(testing, old_media, new_media, "testing media bullet")

    old_guard = (
        "- managed-storage path guards that reject symlinks and Windows junction/reparse entries below configured output/upload roots before generated files or uploaded media are created or resolved; generated-output reads use ancestry-pinned acquisition rather than check-then-reopen pathnames. POSIX descends through descriptor-relative `open/stat` plus `O_NOFOLLOW`, while Windows retains read-share-only component handles opened with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse handles, and denies both write and delete sharing until the final file is acquired"
    )
    new_guard = (
        "- managed-storage path guards that reject symlinks and Windows junction/reparse entries below configured output/upload roots; both reads and new-file writes use ancestry-pinned acquisition rather than check-then-reopen pathnames. POSIX descends through descriptor-relative `open/stat` plus `O_NOFOLLOW` and creates with `O_CREAT|O_EXCL`; Windows retains read-share-only component handles opened with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse handles, denies both write and delete sharing until final acquisition, and creates destinations with `CREATE_NEW`"
    )
    testing = replace_once(testing, old_guard, new_guard, "testing managed-storage bullet")

    old_regression = (
        "The linked managed-storage regression uses only temporary output/upload roots. It proves that ordinary managed IDs remain usable and static symlink/junction/reparse entries are rejected. On POSIX-capable runners it replaces an ID-directory pathname after the original parent descriptor has been acquired and verifies that the final read still comes from the pinned original directory rather than the replacement target; the Ubuntu workflow runs this regression directly. Source-level assertions keep the Windows contract explicit: component handles are opened with `FILE_FLAG_OPEN_REPARSE_POINT`, reparse handles are rejected, and both `FILE_SHARE_WRITE` and `FILE_SHARE_DELETE` are deliberately omitted while the chain is retained. The generated-file integrity regression exercises the actual Flask download route and verifies normal bytes, length/mtime/ETag metadata, byte-range `206` responses, and fallback naming for legacy rows whose `original_filename` is null."
    )
    new_regression = (
        "The linked managed-storage regression uses only temporary output/upload roots. It proves that ordinary managed IDs remain usable and static symlink/junction/reparse entries are rejected. On POSIX-capable runners it replaces an ID-directory pathname after the original parent descriptor has been acquired and verifies both that a final read still comes from the pinned original directory and that a new write is created beneath that pinned directory rather than the replacement target; the Ubuntu workflow runs this regression directly. Source-level assertions keep the Windows contract explicit: component handles are opened with `FILE_FLAG_OPEN_REPARSE_POINT`, reparse handles are rejected, both `FILE_SHARE_WRITE` and `FILE_SHARE_DELETE` are deliberately omitted while the chain is retained, and final creation uses `CREATE_NEW`. The same source contract requires every generated-output writer plus media upload to use the managed create boundary and forbids the former `save(target.full_path)`/`open(target.full_path)` patterns. The generated-file integrity regression exercises the actual Flask download route and verifies normal bytes, length/mtime/ETag metadata, byte-range `206` responses, and fallback naming for legacy rows whose `original_filename` is null."
    )
    testing = replace_once(testing, old_regression, new_regression, "testing linked regression paragraph")
    TESTING.write_text(testing, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
