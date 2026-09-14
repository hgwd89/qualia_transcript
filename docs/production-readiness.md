# Production Readiness Acceptance

## Purpose

`production readiness audit` is the final read-only acceptance gate for real Qualia Transcript data before professional delivery or broad internal use.

Two scopes are supported:

- Database-wide mode is the default and audits every project/business row in the selected SQLite database.
- Project-scoped mode uses `--project-id <ID>` and limits project-owned workflow/content checks to that project. Shared database integrity, declared foreign-key consistency, raw-snapshot structural validation, and backup-set validation remain global because they describe the safety of the common database/recovery set rather than one project's rows.

Project-scoped mode is intended for the normal multi-project workstation case: an unrelated draft project should not block delivery of a different completed project merely because it still has unknown speakers, incomplete mappings, missing draft outputs, or active project-specific jobs.

It is intentionally separate from real-data CI acceptance. CI uses temporary fixtures and cannot inspect the private local research database, raw transcript snapshots, generated deliverables, or backup archives on the research workstation.

## Command

From the repository root, audit the entire database:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_production_readiness.ps1
```

Audit one project while retaining shared database/recovery-set checks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_production_readiness.ps1 --project-id 123
```

For stricter acceptance where warnings also fail:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_production_readiness.ps1 --project-id 123 --strict
```

For machine-readable database-wide output:

```powershell
python scripts/audit_production_readiness_v2.py --json
```

For machine-readable project-scoped output:

```powershell
python scripts/audit_production_readiness_project.py --project-id 123 --json
```

Custom paths can be supplied when validating a copy or recovery environment:

```powershell
python scripts/audit_production_readiness_project.py `
  --project-id 123 `
  --db C:\path\to\qualia_transcript.db `
  --upload-dir C:\path\to\uploads `
  --output-dir C:\path\to\outputs `
  --backup-dir C:\path\to\backups `
  --strict
```

## Safety contract

The audit:

- opens the application SQLite database in `mode=ro`
- uses connection-local TEMP VIEWs for project scoping and does not write them into the application database
- does not call `create_app()`
- does not run migrations
- does not call OpenAI
- does not run Whisper
- does not create Word/Excel outputs
- does not modify `Segment.text`
- does not modify raw transcript snapshots
- does not create or restore backups
- may create temporary local snapshots of the audited SQLite read transaction and managed upload/output files solely to hash and compare the current recovery set; those temporary files are deleted before the audit returns

## Scope semantics

In project-scoped mode, project-owned tables such as interviews, flows/questions, segments, mappings, speaker assignments, AI analyses, generated files, media, and transcriptions are exposed to the existing audit through connection-local TEMP VIEWs limited to the selected project. The original SQLite file remains unchanged.

Project-specific active/orphan `ProcessingJob` findings are filtered to the selected project. By contrast, these checks deliberately remain database-wide even in project mode:

- SQLite `PRAGMA integrity_check`
- declared foreign-key violations from `PRAGMA foreign_key_check`
- existence/schema protection of the shared `processing_jobs` table
- structural/hash validity of the shared raw-transcript snapshot store
- validity/existence of the newest shared local backup archive
- exact freshness of the newest valid backup against the audited DB/uploads/outputs recovery set

Backup freshness is content-based, not timestamp-based. Readiness validates the newest archive and compares its manifest against a temporary snapshot of the current recovery set. Uploads and outputs use exact normalized path membership plus byte size and SHA-256; the database is compared as one logical recovery member using the manifest-declared safe `database_archive_path`, byte size, and SHA-256 rather than requiring one hard-coded ZIP pathname. The database side is materialized from the same already-established SQLite read transaction used by the rest of readiness, so WAL state or a concurrent later commit cannot silently switch the database generation used for the backup comparison.

A project-scoped PASS therefore means the selected project's content/traceability checks passed and the shared persistence/recovery substrate was also acceptable. It does not certify unrelated project content.

## Blocking conditions

A BLOCKER means the audited scope should not be treated as professionally deliverable until resolved.

The audit currently blocks on:

- missing required database tables
- failed SQLite `PRAGMA integrity_check`
- existing SQLite foreign-key violations reported by `PRAGMA foreign_key_check`
- missing `processing_jobs.question_id` on an installation that has not completed the compatibility upgrade
- legacy `processing_jobs.question_id` with neither a declared FK nor the compatibility insert/update trigger guard
- existing `ProcessingJob.question_id` values that reference missing interview-flow questions in the audited project scope
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
- registered `approved_analysis` artifacts whose `artifact_sha256` metadata is missing/invalid or whose pinned managed bytes do not match that registered SHA-256; this is the same byte-integrity condition enforced by the standard formal download route
- a project that already has registered `approved_analysis` history but has no artifact that still matches the current approved-analysis set, exported source-provenance hashes, exact exported analysis/review state, and current canonical source; stale predecessors remain valid history, but at least one current distributable formal artifact must exist once formal history exists
- invalid newest backup archive
- inability to snapshot/compare the current recovery set against the newest valid backup
- a newest valid backup whose logical database snapshot bytes differ from the audited database snapshot, or whose uploads/outputs differ by normalized path membership, byte size, or SHA-256

A foreign-key blocker means the database already contains at least one child row whose referenced parent row is missing. FK enforcement prevents new invalid writes, but it does not repair corruption that predates enforcement; the affected rows must be reconciled before release.

Legacy `processing_jobs` tables require special handling because older SQLite installations added `question_id` after table creation and therefore may not have the model-declared FK. The upgraded app installs non-destructive insert/update trigger guards so future orphan question references are rejected without rebuilding durable job history. Readiness separately scans existing rows so pre-upgrade orphan values remain visible as blockers rather than being silently changed.

Chunk-manifest JSON under `outputs/raw_transcripts/` is metadata and does not satisfy the immutable raw-text snapshot requirement by itself. The hardened audit counts only snapshot payloads that actually contain raw text and validates their recorded hash when present.

Formal approved-analysis artifacts are checked through the same pinned managed-reader and SHA-256 verification boundary used by delivery. The audit remains read-only: it creates only a temporary in-process verification snapshot and never rewrites the registered workbook. Legacy formal artifacts that predate `artifact_sha256` fail closed because the final acceptance gate cannot prove that their bytes are the bytes registered by the formal exporter.

Formal currentness is evaluated separately from byte integrity. Readiness mirrors the delivery contract against the current approved-analysis ID set, each analysis's exported source-provenance SHA-256, the exact formal workbook state hash (including review metadata), and current canonical-source provenance. Old formal files are deliberately retained as history and may be stale; their presence alone is not a blocker. The blocker applies only when a project has formal history but none of its registered formal artifacts is current enough for the standard delivery route.

The backup freshness blocker is deliberately stronger than "a backup exists" or "the ZIP validates." A backup taken before a later database commit, upload replacement, generated-output change, raw-snapshot addition/removal, or any other recovery-set byte change is a valid historical backup but is stale for the current release. Creating a new verified backup of the current DB/uploads/outputs state restores this readiness condition.

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
- active processing jobs that have not reached a terminal state in the audited project scope
- no local backup archive yet

Some warnings may be legitimate during work-in-progress. They should not remain unexplained at final delivery.

## Recommended professional release gate

Before treating one project as ready for delivery:

1. Run `scripts/check_all.ps1 -AllLocal`.
2. Run `scripts/check_local_data_integrity.ps1` with a baseline for important production datasets.
3. Create and validate a backup with `scripts/backup_local_data.py` after the release dataset has reached its intended final state.
4. Run `scripts/check_production_readiness.ps1 --project-id <ID> --strict` against the exact database, uploads, outputs, and backup set you intend to accept. Omit `--project-id` only when you deliberately want whole-database acceptance.
5. Open the final Word/Excel files and compare them with the agreed deliverable template/golden file.
6. Only then copy or send the deliverables outside the workstation.

The backup precedes the strict audit deliberately: `no_backup_archive` is a readiness warning, and `--strict` converts warnings into a non-zero result. On a fresh workstation, running strict readiness before creating the first backup would therefore fail by design. If any DB/upload/output byte changes after the backup, readiness now blocks with `latest_backup_stale`; create a new verified backup before release.

A clean project-scoped strict readiness audit means the selected project's structural/traceability checks, formal-artifact byte-integrity/currentness checks, and the shared database/recovery-set checks passed. It also means the newest valid backup exactly reproduces the audited recovery-set file membership and bytes. It does not replace human qualitative-research review of interpretation quality, moderation context, or client-specific formatting requirements.

## CI coverage

The real-data audit remains manual-only. Required CI includes focused temporary-fixture regressions for readiness foreign-key/orphan behavior, traceability rules such as flow ownership/per-source evidence matching, project-scope isolation, and delivery/readiness parity for formal approved-analysis artifact integrity/currentness. The project-scope regression proves that unrelated project content defects do not leak into the selected project and that the source SQLite bytes remain unchanged. Formal-artifact regressions run on Windows and Ubuntu. They prove current bytes are accepted by both delivery and readiness, in-place tampering is rejected by both, exact-byte restoration returns both gates to acceptance, review-state or approved-set drift leaves old artifacts historical/non-distributable, regeneration restores readiness without deleting stale history, and project-scoped readiness does not import another project's stale formal history.

`tests/smoke_backup_readiness_freshness.py` runs on Windows and Ubuntu. It proves a fresh backup matches the current DB/uploads/outputs recovery set, the comparison remains bound to an already-established SQLite read snapshot, safe manifest-declared noncanonical database member paths remain compatible with restore/validation, later database changes and same-size upload byte changes are detected by SHA-256, current-only and backup-only output membership drift are detected, and creating/restoring the exact current recovery state returns the gate to acceptance.

The broader `tests/smoke_production_readiness.py` uses a temporary SQLite database and temporary outputs to verify the hardened audit, artifact validation, raw-snapshot validation, backup validation, and job-quiescence logic; it remains part of `scripts/check_all.ps1 -AllLocal` rather than reading the real project database.