# Qualia Transcript Testing and Check Guide

## Purpose

This document separates safe local checks from paid/API checks, output-generation checks, AI-analysis review/export checks, and manual local research-data integrity checks. It is intended for Codex, Claude Code, and human reviewers.

`AGENTS.md` remains the top-level rule. If there is a conflict, follow `AGENTS.md`.

## Check Categories

Safe checks must not call OpenAI, run Whisper, depend on existing research-data fixture IDs, or modify source transcript text.

Paid/API checks call external providers or intentionally validate paid paths. Run them only when explicitly requested.

Output-generation checks may create Word, Excel, CSV, or `GeneratedFile` records. Self-contained smoke checks must direct these outputs to temporary directories rather than the repository's real `outputs/` directory.

AI-analysis review/export checks validate the human approval gate and approved-only formal analysis workbook. They use temporary fixtures and temporary output paths and must not call OpenAI or Whisper.

Manual local-data-integrity checks inspect an existing local database and raw transcript snapshots in read-only mode. They are intentionally separate from CI because their result depends on local research data.

## Aggregate Runner

Run the default safe check with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1
```

With no arguments, this runs the safe smoke check only.

Run the self-contained non-paid local suite with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -AllLocal
```

`-AllLocal` runs the safe smoke check, segment-flag smoke, speaker-assignment smoke, output-flag smoke, integrated analysis no-AI check, integrated-analysis CLI guards, AI-analysis review/export smoke, and integrated-analysis preview UI check. These checks use temporary fixtures/directories where applicable and must not depend on existing local research-data IDs.

Other opt-in flags include:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -IntegratedPreview
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Mapping
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Analysis
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Transcription
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Outputs
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -AllPaid
```

`-AllPaid` enables mapping, analysis, transcription, and outputs according to the current script. Confirm provider configuration and API-key handling before running it.

## Safe Checks

Run the safe smoke check for documentation and low-risk operations changes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

Contract:

- uses a temporary SQLite database and temporary upload/output directories
- does not call OpenAI or Whisper
- does not depend on existing local fixture IDs
- verifies the main routes and models can initialize/query
- verifies `.env`, `instance/`, uploads, outputs, raw transcript snapshots, DB files, and logs are not tracked
- leaves the repository's real instance/upload/output directory state unchanged

GitHub Actions runs the same category through `.github/workflows/safe-check.yml` with the `safe-smoke` job.

## Manual Local Data Integrity Check

Use this only when a human wants to inspect the existing local SQLite database and raw transcript snapshots without modifying them:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_local_data_integrity.ps1
```

Contract:

- opens SQLite with `mode=ro`
- does not call `create_app()` or `db.create_all()`
- does not call OpenAI
- does not run Whisper
- does not generate Word or Excel outputs
- checks required tables
- checks `Segment.text` non-emptiness
- checks Segment → Interview and Segment → Transcription references
- checks UtteranceMapping → Segment and → InterviewFlowQuestion references
- checks SegmentFlag → Segment references
- checks SpeakerAssignment → Interview and optional Participant references
- reports a representative `Segment.text` fingerprint
- reports AIAnalysis counts by analysis type
- hashes raw transcript snapshot files without modifying them

An optional JSON baseline can be supplied through `QUALIA_LOCAL_DATA_BASELINE`. Supported baseline keys are:

- `segment_fingerprint`: exact fingerprint of the representative source segment sample
- `minimum_table_counts` (or legacy `table_counts`): minimum acceptable counts, useful for detecting unexpected row loss
- `minimum_ai_analysis_counts` (or legacy `ai_analysis_counts`): minimum counts by analysis type
- `raw_transcript_snapshots`: filename → SHA-256 map; every baseline snapshot must still exist with identical content
- `raw_transcript_snapshot_count`: legacy minimum snapshot count

If the database has no segments and no baseline requiring existing research data, the check reports that the research-data sample is unavailable but still runs structural checks. If a configured baseline expects segments or other rows, an emptied database fails the minimum-count/fingerprint checks instead of silently passing.

This check is manual-only. Do not add it to `.github/workflows/safe-check.yml` and do not make it a required branch-protection check.

## Integrated Analysis No-AI Checks

Run integrated analysis checks only when validating no-AI integrated assembly:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_cli_guards.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_preview_ui.ps1
```

Expected contract:

- no OpenAI API call
- no Whisper execution
- no database save for dry-run preview
- no `Segment.text` change
- no raw transcript change

## AI Analysis Review and Approved Export Check

Run this when changing `AIAnalysis` review state, evidence traceability, the review UI/API, or the approved-only analysis workbook:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_analysis_review.ps1
```

Expected contract:

- temporary SQLite database and temporary output directory only
- no OpenAI or Whisper call
- an AI finding can be approved only when its `evidence_quote` resolves to respondent `Segment` rows inside the analysis scope
- approval persists `source_segment_ids` into the finding payload
- an unresolved evidence quote is rejected by the approval endpoint and remains unapproved
- formal AI-analysis XLSX contains only `review_status=approved` analyses
- the evidence sheet preserves `evidence_quote` and `source_segment_ids`
- `Segment.text` remains unchanged
- generated workbook stays under the temporary output directory

This check is included in `scripts/check_all.ps1 -AllLocal`. It is not part of the minimal required `safe-smoke` workflow.

## Segment Flag Checks

Run segment flag checks when changing flag routes, models, or output reflection:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_flags.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

Expected contract:

- temporary DB fixtures only; no fixed local segment IDs
- flags are stored separately from `Segment.text`
- quote, favorite, exclude, and needs-review behavior remains explicit
- Word and Excel flag reflection remains traceable to segment flags
- output-flag smoke writes generated artifacts only under its temporary output directory
- no OpenAI or Whisper call

## Speaker Assignment Checks

Run speaker assignment checks when changing speaker role logic or UI/API behavior:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_speaker_assignments.ps1
```

Expected contract:

- temporary DB fixtures only; no fixed local interview IDs
- respondent assignments can link to participants
- moderator and observer assignments can remain unlinked from participants
- duplicate upserts do not create duplicate assignment rows
- `Segment.text` remains unchanged

## Output Generation Checks

Run output checks only when Word, Excel, CSV, output flags, or `GeneratedFile` behavior changes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_outputs.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

Expected contract:

- output generation must not modify raw transcripts
- output generation must not modify `Segment.text`
- generated files must stay out of Git
- self-contained output-flag smoke writes only to a temporary output directory
- approved AI-analysis output must never include draft or rejected `AIAnalysis` rows
- inspect `git status --short` before committing after any non-temporary manual output generation

## Paid/API Checks

Run only with explicit approval:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_mapping.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_transcription.ps1
```

Expected behavior:

- `check_mapping.ps1` may call OpenAI for mapping.
- `check_analysis.ps1` may call OpenAI for analysis.
- `check_transcription.ps1` may call OpenAI or transcription providers according to implementation.
- Do not run these in CI or safe checks without explicit approval and API-key handling.

## Semantic Analysis Checks

For semantic analysis, start with dry-run and no-AI:

```powershell
python scripts/run_semantic_analysis.py --interview-id <id> --dry-run --no-ai
```

Do not run semantic analysis with persistence or API-backed behavior unless the task explicitly requires it.

Expected contract:

- `Segment.text` remains unchanged
- raw transcript snapshots remain unchanged
- fragmentation and glossary normalization are derived data only
- source evidence fields remain traceable to source segments

## PR Preflight

Before proposing completion:

```powershell
git status --short
git diff --stat
git diff --check
```

Then run only the checks relevant to the changed files. For broad test-infrastructure changes, run `scripts/check_all.ps1 -AllLocal` locally when PowerShell and the application dependencies are available.

For documentation-only changes, the minimum check is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

If generated files appear in `git status --short`, do not stage them. Identify which command created them and report it.

For GitHub review and branch-protection guidance, also read `docs/github-operations.md`.

## Git Hygiene Checks

Before staging:

```powershell
git status --short
git diff --name-only
```

Confirm these are not staged:

- `.env`
- `instance/`
- `uploads/`
- `outputs/`
- `outputs/raw_transcripts/`
- `*.db`
- `*.db-journal`
- `*.sqlite3-journal`
- `logs/*.log`
- generated Word, Excel, CSV, or transcript files

## Future Test Candidates

- A broader text-analysis contract smoke that verifies evidence fields remain derived and source-safe across every analysis type.
- A speaker-role evidence guard smoke that verifies unknown and interviewer-like speakers are excluded from respondent evidence in all analysis paths.

Prefer extending existing smoke tests when they already cover the same behavior. Avoid duplicate tests that add maintenance cost without increasing coverage.
