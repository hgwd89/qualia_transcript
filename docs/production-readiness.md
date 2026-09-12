# Production Readiness Acceptance

## Purpose

`production readiness audit` is the final read-only acceptance gate for the real Qualia Transcript database snapshot before professional delivery or broad internal use.

The current audit is database-wide, not a single-project selector. Every project and relationship present in the selected SQLite database can contribute blockers or warnings. If one workstation database contains unrelated work-in-progress projects, a strict audit may therefore fail because of those projects as well. Use a clean delivery/recovery copy when a database-wide acceptance result is required for only a subset of work; do not interpret a whole-database PASS as project-scoped certification.

It is intentionally separate from CI. CI uses temporary fixtures and cannot inspect the private local research database, raw transcript snapshots, generated deliverables, or backup archives on the research workstation.

## Command

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_production_readiness.ps1
```

For a stricter acceptance where warnings also fail:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_production_readiness.ps1 --strict
```

For machine-readable output:

```powershell
python scripts/audit_production_readiness_v2.py --json
```

Custom paths can be supplied when validating a copy or recovery environment:

```powershell
python scripts/audit_production_readiness_v2.py `
  --db C:\path\to\qualia_transcript.db `
  --output-dir C:\path\to\outputs `
  --backup-dir C:\path\to\backups `
  --strict
```

## Safety contract

The audit:

- opens SQLite in `mode=ro`
- does not call `create_app()`
- does not run migrations
- does not call OpenAI
- does not run Whisper
- does not create Word/Excel outputs
- does not modify `Segment.text`
- does not modify raw transcript snapshots
- does not create or restore backups

## Blocking conditions

A BLOCKER means the selected database snapshot should not be treated as professionally deliverable until resolved.

The audit currently blocks on:

- missing required database tables
- failed SQLite `PRAGMA integrity_check`
- existing SQLite foreign-key violations reported by `PRAGMA foreign_key_check`
- missing `processing_jobs.question_id` on an installation that has not completed the compatibility upgrade
- legacy `processing_jobs.question_id` with neither a declared FK nor the compatibility insert/update trigger guard
- existing `ProcessingJob.question_id` values that reference missing interview-flow questions
- empty source Segment text
- unsupported speaker roles
- interview/segment/speaker-assignment participant links crossing project boundaries
- mapping to a question when the interview has no assigned flow, when the question/section is missing, or when the question belongs to a different flow
- approved AI analysis with invalid JSON or no findings
- approved findings missing `evidence_quote` or `source_segment_ids`
- approved evidence referencing missing, non-respondent, wrong-project, or wrong-interview Segments
- every cited approved source Segment failing to support the finding's `evidence_quote`; one matching Segment does not legitimize unrelated extra source IDs
- `GeneratedFile` paths escaping the output directory
- registered generated artifacts that are missing or zero bytes
- malformed or hash-invalid raw transcript snapshots
- structurally invalid registered Office artifacts
- invalid newest backup archive

A foreign-key blocker means the database already contains at least one child row whose referenced parent row is missing. FK enforcement prevents new invalid writes, but it does not repair corruption that predates enforcement; the affected rows must be reconciled before release.

Legacy `processing_jobs` tables require special handling because older SQLite installations added `question_id` after table creation and therefore may not have the model-declared FK. The upgraded app installs non-destructive insert/update trigger guards so future orphan question references are rejected without rebuilding durable job history. Readiness separately scans existing rows so pre-upgrade orphan values remain visible as blockers rather than being silently changed.

Chunk-manifest JSON under `outputs/raw_transcripts/` is metadata and does not satisfy the immutable raw-text snapshot requirement by itself. The hardened audit counts only snapshot payloads that actually contain raw text and validates their recorded hash when present.

## Warning conditions

Warnings require human review and become failures under `--strict`.

Current warnings include:

- unknown speaker roles
- respondent Segments with no mapping
- respondent Segments still unclassified
- downstream/final interview status with zero Segments
- completed transcriptions without raw transcript snapshots
- analyzed/done interviews with no approved AI analysis
- generated file extension mismatching recorded format
- active processing jobs that have not reached a terminal state
- no local backup archive yet

Some warnings may be legitimate during work-in-progress. They should not remain unexplained at final delivery.

## Recommended professional release gate

Before treating the selected database snapshot as ready for delivery:

1. Run `scripts/check_all.ps1 -AllLocal`.
2. Run `scripts/check_local_data_integrity.ps1` with a baseline for important production datasets.
3. Create and validate a backup with `scripts/backup_local_data.py`.
4. Run `scripts/check_production_readiness.ps1 --strict` against the database, outputs, and backup set you intend to accept.
5. Open the final Word/Excel files and compare them with the agreed deliverable template/golden file.
6. Only then copy or send the deliverables outside the workstation.

The backup precedes the strict audit deliberately: `no_backup_archive` is a readiness warning, and `--strict` converts warnings into a non-zero result. On a fresh workstation, running strict readiness before creating the first backup would therefore fail by design.

A clean strict readiness audit means the application's database-wide structural and traceability checks passed for the selected snapshot. It does not replace human qualitative-research review of interpretation quality, moderation context, or client-specific formatting requirements.

## CI coverage

The real-data audit remains manual-only. Required CI includes focused temporary-fixture regressions for readiness foreign-key/orphan behavior and traceability rules such as flow ownership and per-source evidence matching. The broader `tests/smoke_production_readiness.py` uses a temporary SQLite database and temporary outputs to verify the hardened audit, artifact validation, raw-snapshot validation, backup validation, and job-quiescence logic; it remains part of `scripts/check_all.ps1 -AllLocal` rather than reading the real project database.
