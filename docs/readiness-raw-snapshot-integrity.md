# Readiness Raw-Snapshot Integrity

Raw transcript JSON is retained research-source evidence. Production readiness evaluates the raw-transcript directory through one pinned batch read so current text self-hashes, deleted-project provenance hashes, tombstone presence, and generation matching are all derived from the same filesystem snapshot.

Current raw text snapshots written by the transcription service include a SHA-256 of the raw text. A present but malformed or mismatched self-hash is a `raw_snapshot_invalid` blocker. Legacy non-tombstoned raw text snapshots without a self-hash remain readable for compatibility, but readiness reports `raw_snapshot_byte_integrity_unproven`; the recommended `--strict` release gate therefore rejects that unproven state without retroactively corrupting legacy data.

When a project is deleted, its raw transcript JSON is intentionally retained and `raw_snapshot_tombstones` stores the SHA-256 of the entire retained JSON file. Readiness treats that database hash as the authoritative integrity proof for the historical source. A tombstoned file that is missing, whose recorded tombstone hash is invalid, or whose current full-file SHA-256 no longer matches the recorded value produces the `raw_snapshot_tombstone_integrity_invalid` blocker.

A tombstoned historical snapshot does not also require an internal text self-hash when its database full-file hash is valid: the external tombstone digest binds the complete JSON bytes more strongly. Chunk-manifest JSON remains metadata and does not satisfy the completed-transcription raw-text evidence requirement.

Project-scoped readiness intentionally keeps raw-snapshot integrity global. Raw snapshots and tombstones are part of the shared evidence/recovery substrate, and a project-scoped acceptance must not silently ignore corruption of retained historical source evidence in that shared store.

`tests/smoke_raw_snapshot_readiness_integrity.py` covers verified current snapshots, legacy unproven self-hash behavior, DB-hash-proven historical snapshots, self-consistent tampering that only the tombstone full-file hash can detect, exact-byte restoration, missing tombstoned source, and present-but-mismatched current self-hashes. The regression runs on Windows and Ubuntu Safe Smoke jobs.
