# GitHub Operations

## Purpose

This document describes the manual GitHub settings and PR workflow needed to protect raw transcript data, `Segment.text`, safe checks, and paid/API boundaries.

`AGENTS.md` is the top-level repository rule. This document is operational guidance for GitHub, Codex, Claude Code, and human reviewers.

## Branch Protection

Configure branch protection manually in GitHub for `master`.

Recommended settings:

- Require a pull request before merging.
- Require at least one human approval.
- Require conversation resolution before merge.
- Require status checks to pass before merge.
- Require branches to be up to date before merge if CI is enabled.
- Do not enable automatic merge for changes touching raw transcript, database, output, analysis, or transcription paths.
- Restrict force pushes.
- Restrict deletions.

Recommended required check:

- `safe-smoke` from `.github/workflows/safe-check.yml`.

Do not make these required by default unless the PR explicitly targets them:

- OpenAI analysis checks.
- OpenAI mapping checks.
- Whisper/transcription checks.
- Bulk Word or Excel output generation checks.
- Checks that require private local fixture data.
- Manual local-data-integrity checks that inspect an existing local database.

## Safe Check Policy

Safe checks must not:

- call OpenAI APIs
- run Whisper
- mutate `Segment.text`
- mutate raw transcript snapshots
- generate large Word or Excel outputs
- require uploaded media or existing local database fixtures
- print `.env` values or API keys

If a check violates one of these rules, it belongs in a manual or paid/API category, not in required CI.

The safe workflow intentionally runs only `scripts/check_safe.ps1`. Do not add mapping, analysis, transcription, or output-generation checks to this workflow.

`scripts/check_local_data_integrity.ps1` is a manual read-only local database check. It is useful for validating a research workstation, but it depends on local data and must not be a required CI check.

## Paid/API and Whisper Checks

Run paid/API checks only by explicit human request.

Examples:

- `scripts/check_mapping.ps1`
- `scripts/check_analysis.ps1`
- `scripts/check_transcription.ps1`

PRs that modify these paths should describe:

- which provider may be called
- expected call count
- required environment variables
- whether local media, database rows, or generated files are touched

## Output Generation Checks

Word, Excel, CSV, and `GeneratedFile` checks are not default safe checks.

Before running output checks:

- confirm the output scope
- confirm whether temporary output paths are used
- inspect `git status --short` after the run
- never stage generated output files

## Raw Transcript and Segment Protection

Treat these as high-risk areas:

- `Segment.text`
- raw transcript snapshots under `outputs/raw_transcripts/`
- uploaded media under `uploads/`
- transcription persistence paths
- semantic analysis persistence
- report generation paths that read source text

PRs touching these areas should explicitly answer:

- Does this change modify source transcript text?
- Does this change modify raw transcript snapshots?
- Does this change preserve source evidence fields?
- Does this change distinguish respondent evidence from moderator, observer, interviewer, and unknown speaker turns?

## Secrets and Artifacts

Secrets must remain in GitHub Secrets or local `.env`, never in repository files or logs.

Before merge, verify that these are not tracked:

- `.env`
- `uploads/`
- `outputs/`
- `outputs/raw_transcripts/`
- `*.db`
- `*.db-journal`
- `logs/*.log`
- generated Word, Excel, CSV, transcript, or analysis output files

## Self-Hosted Runner Notes

If a self-hosted GitHub Actions runner is used:

- keep the runner workspace isolated from real interview data
- avoid mounting production-like uploads, outputs, database files, or raw transcript snapshots
- do not expose `.env` to safe jobs
- run paid/API jobs only through explicit workflow dispatch or protected environments
- clean generated artifacts between jobs
- ensure logs do not print API key values

## Codex Review Workflow

Use Codex review comments for focused review, not broad product discovery.

Suggested PR comment:

```text
@codex review

Focus on:
- whether the implementation satisfies the issue acceptance criteria
- raw transcript and Segment.text safety
- security and secret leakage risks
- paid/API or Whisper checks accidentally mixed into safe checks
- missing tests
- unnecessary complexity
```

Suggested fix comment:

```text
@codex fix the P1 issue

Constraints:
- Keep the PR small.
- Do not change unrelated files.
- Do not call OpenAI or Whisper.
- Add or update tests if behavior changes.
- Explain the fix in the PR comment.
```

## Manual Merge Checklist

Before merging to `master`:

- PR has a clear issue or acceptance criteria.
- Changed files match the stated purpose.
- Safe checks passed.
- Paid/API checks were not run unless explicitly requested.
- Generated files and secrets are not staged.
- Raw transcript snapshots and `Segment.text` were not modified unless explicitly approved.
- Reviewer has checked for data-loss, migration, regression, and evidence-trace risks.
