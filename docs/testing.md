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
- launcher runtime configuration: IPv4/IPv6 bind addresses resolve to reachable browser/readiness URLs, IPv4/IPv6 wildcard binds map to loopback destinations, and IPv6 literals are RFC-3986 bracketed in URLs
- generated-file integrity, including project-scoped path enforcement, UUID-isolated internal storage for same-name outputs, rollback isolation, bounded storage basenames for long display filenames, and rejection of linked/reparse project storage paths
- media-upload integrity, including rejection of linked/reparse interview storage paths
- managed-storage path guards that reject symlinks and Windows junction/reparse entries below configured output/upload roots before generated files or uploaded media are created or resolved
- backup/restore integrity hardening: the backup tool resolves the same SQLite file as Flask, validates and restores the same staged archive bytes, honors the manifest-declared database member, removes a newly created DB when restore rolls back, preserves a raw damaged DB copy only under explicit recovery acknowledgement, and keeps backup files owner-only on POSIX
- runtime maintenance exclusion: the local Flask process and detached durable workers hold shared runtime file locks for their full lifetimes; backup and applied restore hold the exclusive maintenance lock for the whole operation; app and worker locks can coexist, but maintenance is rejected while either remains alive, including when a worker outlives the Flask process; cross-process behavior is exercised on Windows CI and POSIX-compatible locking is implemented
- project deletion lifecycle, including durable-job serialization, managed-storage cleanup, non-recursive linked-path handling, DB-failure preservation, and mandatory retention of raw transcript snapshots
- participant/interview-flow project boundaries and delete guards
- interview-creation scope validation
- SQLite foreign-key enforcement
- legacy `processing_jobs.question_id` compatibility guards: insert/update triggers must reject orphan question references even when `PRAGMA foreign_keys=OFF`
- readiness detection of declared SQLite FK violations and explicit `ProcessingJob.question_id` orphans
- readiness traceability regressions: question mappings are rejected when an interview has no assigned flow, and every approved `source_segment_id` must individually support the finding's `evidence_quote`
- project-scoped readiness isolation: `--project-id` excludes another project's workflow/content defects while keeping shared database integrity and recovery-set checks global, and the source SQLite file remains byte-for-byte unchanged
- resumed project-analysis UI recovery: the shared job script must wrap `pollAnalysisJob` before the page's resume handler runs and must re-enable the associated analysis button before propagating a polling error
- Windows DPAPI secret-store behavior

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

This check is manual-only and must not become a required CI gate. It opens SQLite read-only and checks important table relationships, source fingerprints, analysis counts, and snapshot hashes without modifying the source dataset.

## Integrated analysis no-AI checks

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_cli_guards.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_preview_ui.ps1
```

Expected contract: no external request, no save for dry-run preview, no `Segment.text` change, and no raw transcript change.

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

Generated artifacts must stay out of Git and output generation must not modify source transcripts.

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

`Segment.text` and raw snapshots must remain unchanged. Fragmentation and normalization are derived data. Provider-backed semantic execution must use the application's protected settings boundary.

## PR preflight

Before proposing completion:

```powershell
git status --short
git diff --stat
git diff --check
```

Run checks relevant to the changed files. For broad test-infrastructure changes, run `scripts/check_all.ps1 -AllLocal` when the local dependencies are available. Generated/private runtime files must not be staged.

For GitHub review and branch-protection guidance, also read `docs/github-operations.md`.
