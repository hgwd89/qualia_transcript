# Qualia Transcript Testing and Check Guide

## Purpose

This guide defines the required safe test gate and the optional local/provider-backed checks. `AGENTS.md` remains the top-level rule.

## Required safe gate

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

The same required category is run by `.github/workflows/safe-check.yml` as `safe-smoke` and by `scripts/check_all.ps1` with no flags.

Safe checks use temporary fixtures where data or storage is needed. They must not depend on existing research-data IDs, must not change `Segment.text` or raw transcript snapshots, and must leave the repository's real runtime data directories unchanged.

The required `safe-smoke` gate currently covers:

- baseline application/model/route initialization
- launcher runtime configuration and identity, including `0.0.0.0 -> 127.0.0.1`, IPv6 unspecified `:: -> ::1`, RFC-compliant bracketed IPv6 URLs, unchanged hostname/IPv4 URL behavior, exact-repository `app.py` process recognition, rejection of generic/other-project `python app.py` identities, Qualia-specific settings-page endpoint markers for start/readiness checks, exact-process-only stop identity, and force-stop fencing to the originally identified listener PID
- generated-file integrity, including project-scoped path enforcement, UUID-isolated internal storage for same-name outputs, rollback isolation, bounded storage basenames for long display filenames, and rejection of linked/reparse project storage paths
- media-upload integrity, including rejection of linked/reparse interview storage paths
- managed-storage path guards that reject symlinks and Windows junction/reparse entries below configured output/upload roots before generated files or uploaded media are created or resolved
- backup/restore integrity hardening: the backup tool resolves the same SQLite file as Flask, validates and restores the same staged archive bytes, honors the manifest-declared database member, removes a newly created DB when restore rolls back, preserves a raw damaged DB copy only under explicit recovery acknowledgement, keeps backup files owner-only on POSIX, rejects linked/junction/reparse entries inside upload/output backup trees rather than following them, and enforces the exclusive maintenance boundary inside the backup/restore service API itself whenever any configured live recovery-set target is involved
- runtime maintenance exclusion: canonical `create_app()` launches retain a shared runtime slot for the complete process lifetime before DB/schema/storage initialization, including alternate Flask/factory launch paths; detached durable workers, semantic-analysis CLI, operational read-only readers, and explicit processing-job recovery writes share the same runtime slot range; backup and applied restore hold the exclusive maintenance lock for the whole operation. Read-only job inspection, integrated-analysis dry-run, the production-readiness wrappers, all three direct readiness audit entry points (`audit_production_readiness.py`, `_v2.py`, and `_project.py`), and both wrapper/direct manual local-data integrity entry points therefore refuse to start while maintenance is replacing or snapshotting the live recovery set
- project deletion lifecycle, including durable-job serialization, pre-commit quarantine of existing output/upload numeric ID entries and captured processing-job logs, deletion refusal when quarantine cannot be established, quarantine restoration on DB rollback, non-recursive linked-path handling, immediate SQLite Project/ProcessingJob ID-reuse safety after post-commit cleanup failure, and mandatory retention of raw transcript snapshots
- participant/interview-flow project boundaries and delete guards
- interview-creation scope validation
- segment-role normalization/update integrity: canonical `moderator` handling, malformed or empty JSON rejection without mutation, and partial role updates that preserve an existing participant unless `participant_id` is explicitly supplied
- SQLite foreign-key enforcement
- legacy `processing_jobs.question_id` compatibility guards: insert/update triggers must reject orphan question references even when `PRAGMA foreign_keys=OFF`
- readiness detection of declared SQLite FK violations and explicit `ProcessingJob.question_id` orphans
- readiness traceability regressions: question mappings are rejected when an interview has no assigned flow, and every approved `source_segment_id` must individually support the finding's `evidence_quote`
- project-scoped readiness isolation: `--project-id` excludes another project's workflow/content defects while keeping shared database integrity and recovery-set checks global, and the source SQLite file remains byte-for-byte unchanged
- resumed project-analysis UI recovery: the shared job script must wrap `pollAnalysisJob` before the page's resume handler runs and must re-enable the associated analysis button before propagating a polling error
- Windows DPAPI secret-store behavior

The launcher identity regression is pure PowerShell/static validation; it does not start or stop a real process. Start/readiness logic may accept a listener only when its HTTP endpoint positively identifies the configured Qualia Transcript service; generic HTTP 200 is deliberately insufficient. Stop is stricter: endpoint identity alone is not enough. The listener must be a Python process whose command line contains this repository's resolved absolute `app.py` path. Generic `python app.py`, another checkout's `app.py`, and non-Python processes are rejected. The forced-stop path is additionally restricted to the original listener PID plus that exact repository process identity, so a replacement process that acquires the same port is never killed by the retry loop.

The backup service-boundary regression uses only temporary database/upload/output/backup/lock paths. It proves that direct `services.local_backup.create_backup()` and applied `restore_backup()` calls cannot bypass an active runtime holder when pointed at the configured live recovery set, that a refused restore leaves the live database unchanged, and that a linked/reparse entry in a managed backup tree is rejected before it can be copied into an archive. Custom all-temporary fixture targets remain available without acquiring the live maintenance lock.

The app-factory runtime regression uses only temporary database/upload/output/lock paths. It proves that a canonical factory launch keeps maintenance blocked after `create_app()` returns, that the lock disappears when the process exits, and that an exclusive maintenance holder causes factory startup to fail before database or managed-storage initialization.

The Windows-only DPAPI regression reports a skip/pass on non-Windows systems. On Windows it validates local protection/migration behavior, verifies that covered application consumers receive decrypted usable settings without contacting an external service, and verifies settings-form atomicity: if a later secret field cannot be protected, earlier secret and non-secret field changes from the same POST are rolled back together rather than partially committed.

## Broader non-paid local suite

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -AllLocal
```

This adds segment flags, speaker assignments, output flags, integrated no-AI checks, CLI guards, AI-analysis review/export, and integrated preview checks. These checks must use temporary fixtures/directories where applicable.

## Manual local data integrity

Use only for read-only inspection of the existing local research database and transcript snapshots:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_local_data_integrity.ps1
```

This check is manual-only and must not become a required CI gate. It opens SQLite read-only and checks important table relationships, source fingerprints, analysis counts, and snapshot hashes without modifying the source dataset. Both the PowerShell wrapper and direct `python tests/smoke_local_data_integrity.py` entry point hold the shared `reader` runtime lock, so neither can inspect the live DB while backup/applied-restore maintenance owns the exclusive recovery-set boundary.

## Integrated analysis no-AI checks

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_cli_guards.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_preview_ui.ps1
```

Expected contract: no external request, no save for dry-run preview, no `Segment.text` change, and no raw transcript change. Direct integrated-analysis CLI execution holds the shared `reader` runtime lock while it reads the live database.

## AI analysis review/export

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_analysis_review.ps1
```

Use temporary fixtures and output paths. Approval must require resolvable respondent evidence and formal exports must remain approved-only.

## Segment flags

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_flags.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

Flags remain derived data separate from `Segment.text`; self-contained output checks write only to temporary directories.

## Speaker assignments

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_speaker_assignments.ps1
```

Use temporary fixtures. Duplicate upserts must not create duplicate assignment rows.

## Output generation

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_outputs.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

Generated artifacts must stay out of Git and output generation must not modify source transcripts. The output check also runs a self-contained formatted-sheet regression proving that human `SpeakerAssignment` roles override stale `Segment.speaker_role` values for both mapped and unclassified respondent rows.

## Provider-backed checks

Run only when explicitly requested:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_mapping.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_transcription.ps1
```

These are not part of safe-smoke or the normal CI path.

## Semantic analysis

Start with dry-run/no-AI:

```powershell
python scripts/run_semantic_analysis.py --interview-id <id> --dry-run --no-ai
```

`Segment.text` and raw snapshots must remain unchanged. Fragmentation and normalization are derived data. Provider-backed semantic execution must use the application's protected settings boundary. The semantic CLI holds the shared runtime lock even for dry-run because application initialization may perform idempotent schema/secret migration; it therefore refuses to start while backup/applied-restore maintenance owns the exclusive lock.

## PR preflight

Before proposing completion:

```powershell
git status --short
git diff --stat
git diff --check
```

Run checks relevant to the changed files. For broad test-infrastructure changes, run `scripts/check_all.ps1 -AllLocal` when the local dependencies are available. Generated/private runtime files must not be staged.

For GitHub review and branch-protection guidance, also read `docs/github-operations.md`.
