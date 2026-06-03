# Qualia Transcript Testing and Check Guide

## Purpose

This document separates safe local checks from paid/API checks, output-generation checks, and reversible-write checks. It is intended for Codex, Claude Code, and human reviewers.

`AGENTS.md` remains the top-level rule. If there is a conflict, follow `AGENTS.md`.

## Check Categories

Safe checks should not call OpenAI, should not run Whisper, should not generate large output files, and should not modify source transcript text.

Paid/API checks call external providers or intentionally validate paid paths. Run them only when explicitly requested.

Output-generation checks may create Word, Excel, CSV, or `GeneratedFile` records. Run them only when output behavior is the task.

Reversible-write checks may create and restore local database rows. They are not fully read-only and should not be treated as default safe checks unless they have been made self-contained in the current branch.

## Current Aggregate Runner

On current `master`, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1
```

With no arguments, this runs the safe smoke check only.

Opt-in flags on current `master` include:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -IntegratedPreview
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Mapping
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Analysis
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Transcription
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Outputs
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -AllPaid
```

`-AllPaid` enables mapping, analysis, transcription, and outputs according to the current script. Confirm script behavior before running it.

## Safe Checks

Run the safe smoke check for documentation and low-risk operations changes:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

This check must not call OpenAI or Whisper. If it starts requiring an external provider, treat that as a regression.

## Integrated Analysis No-AI Checks

Run integrated analysis checks only when validating no-ai integrated assembly:

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

## Segment Flag Checks

Run segment flag checks only when changing flag routes, models, or output reflection:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_flags.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

Expected contract:

- flags are stored separately from `Segment.text`
- quote, favorite, exclude, and needs-review behavior remains explicit
- Word and Excel flag reflection remains traceable to segment flags
- no OpenAI or Whisper call

On current `master`, verify whether these tests use fixed fixtures or self-contained temporary databases before running them on sensitive local data.

## Speaker Assignment Checks

Run speaker assignment checks when changing speaker role logic or UI/API behavior:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_speaker_assignments.ps1
```

Expected contract:

- respondent assignments can link to participants
- moderator, observer, and unknown speakers are not silently treated as participants
- missing speaker assignment must not be guessed as respondent evidence
- `Segment.text` remains unchanged

On current `master`, verify whether this test uses fixed fixtures or self-contained temporary databases before running it on sensitive local data.

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
- if output files are generated, inspect `git status --short` before committing

## Paid/API Checks

Run only with explicit approval:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_mapping.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_transcription.ps1
```

Expected behavior:

- `check_mapping.ps1` may call OpenAI once for mapping.
- `check_analysis.ps1` may call OpenAI once for analysis.
- `check_transcription.ps1` may call OpenAI or transcription providers according to implementation.
- Do not run these in CI or safe checks without explicit approval and API-key handling.

## Semantic Analysis Checks

For semantic analysis, start with dry-run and no-ai:

```powershell
python scripts/run_semantic_analysis.py --interview-id <id> --dry-run --no-ai
```

Do not run semantic analysis with persistence or API-backed behavior unless the user explicitly asks for that scope.

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

Then run only the checks relevant to the changed files.

For documentation-only changes, the minimum check is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

If generated files appear in `git status --short`, do not stage them. Identify which command created them and report it.

## Git Hygiene Checks

Before staging:

```powershell
git status --short
git diff --name-only
```

Confirm these are not staged:

- `.env`
- `uploads/`
- `outputs/`
- `outputs/raw_transcripts/`
- `*.db`
- `*.db-journal`
- `logs/*.log`
- generated Word, Excel, CSV, or transcript files

## Future Test Candidates

- A text-analysis contract smoke that verifies evidence fields are preserved and analysis data remains derived.
- A segment-integrity smoke that verifies mappings and flags do not mutate segment text.
- A speaker-role guard smoke that verifies unknown and interviewer-like speakers are excluded from respondent evidence.
- An analysis-output contract smoke that verifies required output columns and quote markers without changing raw transcript data.

Prefer extending existing smoke tests when they already cover the same behavior. Avoid duplicate tests that add maintenance cost without increasing coverage.
