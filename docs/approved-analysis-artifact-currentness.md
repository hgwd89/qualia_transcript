# Approved AI Analysis Artifact Currentness

## Purpose

`approved_analysis` XLSX files are immutable generated history, but the normal professional-delivery path must not treat an old workbook as current after the canonical research state, the reviewed analysis state, or the approved-analysis set changes.

Generation-time analysis provenance protects new approval and new formal export. Artifact currentness extends that contract to already-generated formal workbooks so a user cannot bypass the current export gate by downloading an older registered file.

## Currentness contract

A registered `GeneratedFile` with `file_type=approved_analysis` is distributable through the standard output/download route only when all of the following remain true:

1. `generation_params_json` proves it was generated as `approved_only` and contains a non-empty `analysis_ids` list, the `source_provenance_sha256` map, and the `formal_analysis_state_sha256` map recorded by the formal exporter.
2. The recorded analysis IDs exactly equal the project's current `review_status=approved` analysis set. Adding a newly approved analysis, rejecting an included analysis, or otherwise changing that set makes the old workbook historical.
3. Every referenced analysis still belongs to the same project and is still approved.
4. Every recorded source-provenance hash equals the hash stored with that analysis at generation time.
5. Every recorded formal-analysis-state hash still matches the exact analysis/review fields represented by the workbook, including title, participant/question labels, summary, implications, unresolved text, findings/evidence fields, review status/note/time, model, and creation time. Re-approving an analysis with changed review metadata therefore makes an older workbook historical even when its research source is unchanged.
6. Every included analysis still passes the live `analysis-input-v1` canonical-source provenance check.

Legacy formal artifacts that predate the complete provenance/state metadata fail closed because their currentness cannot be proven. They remain stored as historical records; this gate does not delete generated files.

## Download serialization

For a formal approved-analysis download, SQLite acquires `BEGIN IMMEDIATE`, reloads the `GeneratedFile`, validates the complete artifact-currentness contract, and pins the exact managed-file generation before releasing the database write reservation. This prevents a canonical-source edit from entering between validation and acquisition of the bytes that will be served.

Non-formal generated files keep the existing managed-file download behavior.

## UI behavior

The output screen separates persisted human review state from current formal validity:

- `承認状態` counts rows whose persisted `review_status` is `approved`.
- `現在有効` counts approved rows whose generation-time source provenance still matches canonical data.
- Formal generation is enabled only when at least one approved analysis exists and every approved row is currently provable.
- Previously generated formal files are shown as either `現在有効` or `履歴・配布不可`.
- Historical/unprovable formal artifacts have no standard download action.

The report writer and download route remain authoritative. UI currentness is advisory and exists to avoid presenting stale rows as deliverable.

## Regression gate

`tests/smoke_approved_artifact_currentness.py` is providerless and uses a temporary SQLite database/output tree. It verifies:

- current formal artifacts download their exact registered bytes;
- legacy formal artifacts without complete generation provenance/state metadata are rejected;
- canonical-source drift blocks a previously generated formal artifact;
- review-state drift blocks an older artifact even when canonical source is unchanged;
- ordinary generated files remain downloadable;
- the output screen distinguishes current versus stale approved state;
- changing the current approved-analysis set makes an older workbook historical;
- the formal exporter records both source-provenance and formal-analysis-state hashes.

`.github/workflows/safe-check.yml` runs this regression on both Windows and Ubuntu.