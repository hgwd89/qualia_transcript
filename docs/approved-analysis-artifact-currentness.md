# Approved AI Analysis Artifact Currentness

## Purpose

`approved_analysis` XLSX files are immutable generated history, but the normal professional-delivery path must not treat an old or modified workbook as current after the canonical research state, the reviewed analysis state, the approved-analysis set, or the registered workbook bytes change.

Generation-time analysis provenance protects new approval and new formal export. Artifact currentness extends that contract to already-generated formal workbooks so a user cannot bypass the current export gate by downloading an older or modified registered file.

## Currentness contract

A registered `GeneratedFile` with `file_type=approved_analysis` is distributable through the standard output/download route only when all of the following remain true:

1. `generation_params_json` proves it was generated as `approved_only` and contains a non-empty `analysis_ids` list, the `source_provenance_sha256` map, the `formal_analysis_state_sha256` map, and the `artifact_sha256` recorded by the formal exporter.
2. The recorded analysis IDs exactly equal the project's current `review_status=approved` analysis set. Adding a newly approved analysis, rejecting an included analysis, or otherwise changing that set makes the old workbook historical.
3. Every referenced analysis still belongs to the same project and is still approved.
4. Every recorded source-provenance hash equals the hash stored with that analysis at generation time.
5. Every recorded formal-analysis-state hash still matches the exact analysis/review fields represented by the workbook, including title, participant/question labels, summary, implications, unresolved text, findings/evidence fields, review status/note/time, model, and creation time. Re-approving an analysis with changed review metadata therefore makes an older workbook historical even when its research source is unchanged.
6. Every included analysis still passes the live `analysis-input-v1` canonical-source provenance check.
7. The actual managed workbook bytes must match the registered `artifact_sha256` before any response bytes are exposed.

Legacy formal artifacts that predate the complete provenance/state/byte-hash metadata fail closed because their currentness cannot be proven. They remain stored as historical records; this gate does not delete generated files.

## Generation byte binding

After the XLSX writer closes, the formal exporter reopens the managed output through the pinned managed-read boundary, requires the file generation to equal the writer's recorded generation, computes SHA-256 over those exact bytes, and rechecks the file generation after hashing. The normal generated-file registrar then independently re-pins the same writer generation across the database commit window. A replacement or in-place mutation between write, hashing, and registration therefore fails before a distributable `GeneratedFile` row is returned.

## Download serialization and immutable response snapshot

For a formal approved-analysis download, SQLite acquires `BEGIN IMMEDIATE`, reloads the `GeneratedFile`, validates the complete artifact-currentness contract, and opens the managed file through the pinned reader. While the database reservation is still held, the route copies the pinned bytes into a `SpooledTemporaryFile` while calculating SHA-256. A hash mismatch, size change, or generation change aborts the download with a fail-closed response.

Only the verified spool is sent to the client. The original managed handle is closed before the database reservation is released. Consequently, even on POSIX systems where another process can later write to the same inode, the response bytes cannot change after verification.

Non-formal generated files keep the existing managed-file download behavior.

## UI behavior

The output screen separates persisted human review state from current formal validity:

- `承認状態` counts rows whose persisted `review_status` is `approved`.
- `現在有効` counts approved rows whose generation-time source provenance still matches canonical data.
- Formal generation is enabled only when at least one approved analysis exists and every approved row is currently provable.
- Previously generated formal files are shown as either `現在有効` or `履歴・配布不可` based on database currentness metadata. Actual byte integrity remains authoritatively checked at download time.
- Historical/unprovable formal artifacts have no standard download action.

The report writer and download route remain authoritative. UI currentness is advisory and exists to avoid presenting stale rows as deliverable.

## Regression gate

`tests/smoke_approved_artifact_currentness.py` is providerless and uses a temporary SQLite database/output tree. It verifies:

- current formal artifacts download their exact registered bytes;
- legacy formal artifacts without complete generation provenance/state/hash metadata are rejected;
- canonical-source drift blocks a previously generated formal artifact;
- review-state drift blocks an older artifact even when canonical source is unchanged;
- ordinary generated files remain downloadable;
- the output screen distinguishes current versus stale approved state;
- changing the current approved-analysis set makes an older workbook historical;
- in-place modification of a registered formal artifact is rejected by SHA-256 verification;
- a verified response snapshot remains byte-for-byte unchanged after later mutation of the managed file;
- restoring the exact registered bytes restores distributability when all database currentness conditions still hold;
- the formal exporter records source-provenance, formal-analysis-state, and artifact-byte hashes.

`.github/workflows/safe-check.yml` runs this regression on both Windows and Ubuntu.