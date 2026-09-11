# Production Readiness Acceptance

## Purpose

`production readiness audit` is the final read-only acceptance gate for real Qualia Transcript project data before professional delivery or broad internal use.

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
python scripts/audit_production_readiness.py --json
```

Custom paths can be supplied when validating a copy or recovery environment:

```powershell
python scripts/audit_production_readiness.py `
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

A BLOCKER means the dataset should not be treated as professionally deliverable until resolved.

The audit currently blocks on:

- missing required database tables
- failed SQLite `PRAGMA integrity_check`
- empty source Segment text
- unsupported speaker roles
- interview/segment/speaker-assignment participant links crossing project boundaries
- mapping to a question outside the interview's assigned flow
- approved AI analysis with invalid JSON or no findings
- approved findings missing `evidence_quote` or `source_segment_ids`
- approved evidence referencing missing, non-respondent, wrong-project, or wrong-interview Segments
- approved evidence quote not matching its referenced source Segments
- `GeneratedFile` paths escaping the output directory
- registered generated artifacts that are missing or zero bytes
- malformed raw transcript snapshot JSON

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
- no local backup archive yet

Some warnings may be legitimate during work-in-progress. They should not remain unexplained at final delivery.

## Recommended professional release gate

Before treating a project as ready for delivery:

1. Run `scripts/check_all.ps1 -AllLocal`.
2. Run `scripts/check_local_data_integrity.ps1` with a baseline for important production datasets.
3. Run `scripts/check_production_readiness.ps1 --strict`.
4. Create and validate a backup with `scripts/backup_local_data.py`.
5. Open the final Word/Excel files and compare them with the agreed deliverable template/golden file.
6. Only then copy or send the deliverables outside the workstation.

A clean strict readiness audit means the application's structural and traceability checks passed. It does not replace human qualitative-research review of interpretation quality, moderation context, or client-specific formatting requirements.

## CI coverage

The real-data audit remains manual-only. `tests/smoke_production_readiness.py` uses a temporary SQLite database and temporary outputs to verify the audit logic itself. That smoke is included in `scripts/check_all.ps1 -AllLocal` and must not read the real project database.
