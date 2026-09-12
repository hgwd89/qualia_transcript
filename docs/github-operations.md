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
- The broader `-AllLocal` suite; it is a useful manual preflight but is intentionally separate from the minimal required CI gate.

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

The required safe workflow intentionally runs only `scripts/check_safe.ps1`. That script includes lightweight temporary-fixture integrity checks such as `GeneratedFile` path/registration/rollback safety; these do not generate production Word/Excel deliverables and are part of the required safe gate. Do not add provider-backed mapping, analysis, transcription, or production-like output-generation checks to this required workflow.

`scripts/check_all.ps1 -AllLocal` is a broader non-paid local preflight using self-contained temporary fixtures/directories where applicable. It remains manual/non-required so required CI stays small and does not expand into production-like output generation or broader integration coverage.

`scripts/check_local_data_integrity.ps1` is a separate manual read-only local research-data check. It can compare structural integrity, minimum counts, source-segment fingerprints, and raw transcript snapshot hashes against an optional baseline. It depends on local data and must not be a required CI check.

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

Full Word, Excel, CSV, and production-like report generation is not part of the default required safe gate. These checks may create real document artifacts and remain opt-in/manual unless a task explicitly targets them.

The required safe gate does include `tests/smoke_generated_file_integrity.py`. That smoke uses a temporary SQLite database and temporary output root to validate `GeneratedFile` storage-path containment, registration/rollback behavior, collision isolation, and filename safety without producing production deliverables.

Before running production-like output checks:

- confirm the output scope
- confirm whether temporary output paths are used
- inspect `git status --short` after the run
- never stage generated output files

The self-contained output-flag smoke used by `-AllLocal` writes only to a temporary output directory. Manual production-like output checks remain opt-in.

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
- `instance/`
- `uploads/`
- `outputs/`
- `outputs/raw_transcripts/`
- `*.db`
- `*.db-journal`
- `*.sqlite3-journal`
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
- `safe-smoke` passed on the current PR head.
- Relevant manual checks were run where the environment permits them.
- Paid/API checks were not run unless explicitly requested.
- Generated files and secrets are not staged.
- Raw transcript snapshots and `Segment.text` were not modified unless explicitly approved.
- Reviewer has checked for data-loss, migration, regression, and evidence-trace risks.
