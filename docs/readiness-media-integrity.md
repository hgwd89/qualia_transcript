# Readiness Source-Media Integrity

Uploaded audio/video is a research source record, not merely a transient provider input. New `MediaFile` rows persist `file_size_bytes` and `content_sha256` from the exact ancestry-pinned generation committed at upload time, and transcription already verifies those values while creating its private provider snapshot.

Production readiness verifies the retained source media as well. Each current `media_files` row is opened through the ancestry-pinned managed-read boundary under the selected uploads root; registered size is checked when present and registered SHA-256 is verified against that exact open generation. Missing, unsafe, size-mismatched, malformed-hash, or hash-mismatched media is a `media_file_byte_integrity_invalid` blocker.

The global and project-scoped readiness CLIs accept `--upload-dir`, alongside `--db`, `--output-dir`, and `--backup-dir`. When `--upload-dir` is omitted, readiness remains backward-compatible and uses `config.UPLOAD_DIR`. When an alternate recovery set is audited, its uploads directory must be passed explicitly so database rows and retained media are proven against the same recovery set rather than the process's configured live uploads tree. The resolved uploads root is recorded in `report.info.upload_dir`.

Legacy rows with no `content_sha256` remain compatibility-readable but are reported as `media_file_byte_integrity_unproven` warnings. The current managed path must still exist and any registered size must still match; legacy compatibility does not turn a missing source file into an acceptable state.

Project-scoped readiness reuses its existing `media_files` TEMP VIEW, so the same check applies only to media owned by the requested project while global readiness checks the full dataset. The explicit uploads root is propagated unchanged through the project wrapper.

`tests/smoke_media_readiness_integrity.py` covers configured-root fallback, explicit alternate-recovery uploads, project-scoped propagation, verified source media, legacy-unproven compatibility, same-path byte tampering, exact-byte restoration, and missing source media. The regression runs on Windows and Ubuntu in `.github/workflows/safe-check.yml`.
