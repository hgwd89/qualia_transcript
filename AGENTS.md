# AGENTS.md

## Project

This repository is `Qualia Transcript`, a local Flask application for qualitative research interview management, transcription, and analysis.

## Operating principles

- Prefer small, reversible changes.
- Do not expand scope without explicit user approval.
- Before editing, inspect the relevant files.
- After editing, run only the minimum checks relevant to the changed files.
- Keep reports concise and factual.
- Do not expose secrets from `.env`, logs, or command output.
- Do not print API key values. Report keys only as `set` or `not set`.

## Cross-agent documentation

- `CLAUDE.md` is a companion guide for Claude Code. `AGENTS.md` remains the highest-priority repository rule.
- `docs/architecture.md` describes the current app structure and raw-data versus derived-data boundaries.
- `docs/testing.md` describes safe checks, paid/API checks, output-generation checks, and PR preflight checks.
- `docs/github-operations.md` describes GitHub branch protection, PR review, CODEOWNERS, and Codex prompt usage.
- When architecture, check behavior, or quality gates change, update the relevant documentation in the same PR.
- Text analysis, segmentation, semantic analysis, and output generation must preserve source traceability and must not overwrite raw transcript data or `Segment.text`.

## Do not touch unless explicitly requested

Do not modify these areas during launcher, README, or operations tasks:

- `app.py`
- `config.py`
- `routes/`
- `models/`
- `services/`
- `templates/`
- `requirements.txt`
- `.env`

## Secrets and generated files

- Never print API key values.
- Report API keys only as `set` or `not set`.
- `.env` must remain untracked.
- `uploads/`, `outputs/`, `*.db`, `logs/*.log`, virtual environments, and caches must remain untracked.
- Do not add uploaded audio/video files to Git.
- Do not add generated Word, Excel, CSV, transcript, or analysis output files to Git.
- `outputs/raw_transcripts/*.json` is high-sensitivity generated data (raw API transcript snapshot).
- Do not add raw transcript snapshots to Git.
- Do not delete raw transcript snapshots unless the user explicitly requests it.
- When summarizing or auditing raw transcripts, quote only the minimum necessary excerpts.
- If logs are needed for debugging, show only relevant non-secret excerpts.

## Local launcher task rules

For Windows local launcher work, only edit:

- `start_app.ps1`
- `stop_app.ps1`
- `open_app.ps1`
- `scripts/runtime_config.ps1`
- `scripts/check_runtime_config.ps1`
- `README_LOCAL.md`
- `.gitignore`
- `logs/.gitkeep`
- `AGENTS.md`

### Requirements

- `start_app.ps1` must start the repository's `app.py` with Python in the background and pass the resolved absolute script path to the child process.
- It must read `APP_HOST` / `APP_PORT` through `scripts/runtime_config.ps1` rather than hard-code port 5000.
- It must log to `logs/flask_out.log` and `logs/flask_err.log`.
- It may treat an existing listener as Qualia Transcript only when the endpoint positively identifies the configured service; a generic HTTP 200 is insufficient.
- It must wait for readiness with a retry loop: max 30 seconds, 1-second interval, success only when the Qualia endpoint identity check passes.
- It must not kill unrelated processes.
- If a newly launched process fails readiness, cleanup must terminate only the `System.Diagnostics.Process` object returned by that `Start-Process` call; it must not re-resolve the numeric PID for termination.
- `stop_app.ps1` may stop only one unambiguous Python listener on the configured `APP_PORT` whose command line contains this repository's resolved absolute `app.py` as a complete argument. Before both normal and forced termination, it must revalidate the same process instance using PID, process start time, and exact repository `app.py` identity, and terminate through that validated process object. PID reuse or unreadable/inconclusive identity must never cause an unrelated process to be stopped; ambiguous identity fails closed.
- `open_app.ps1` must open the runtime-config URL. Browser URLs normalize wildcard bind hosts (`0.0.0.0` → `127.0.0.1`, `::` → `::1`) and bracket IPv6 literals.

## Lightweight verification

For launcher-only changes, run only:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_runtime_config.ps1
.\start_app.ps1
. (Join-Path $PWD "scripts\runtime_config.ps1")
$pythonExe = Get-QualiaPythonExecutable -ProjectDir $PWD
$runtime = Get-QualiaRuntimeConfig -ProjectDir $PWD -PythonExe $pythonExe
Invoke-WebRequest $runtime.Url -UseBasicParsing
Invoke-WebRequest "$($runtime.Url)settings" -UseBasicParsing
.\open_app.ps1
.\stop_app.ps1
git status
git diff --stat
```

For safe non-destructive regression checks, run:

```powershell
python tests/smoke_safe.py
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

`scripts/check_analysis.ps1` / `tests/smoke_analysis.py` are paid API checks.
- Do not mix them into safe checks.
- Do not run them unless the user explicitly asks.

`scripts/check_mapping.ps1` / `tests/smoke_mapping.py` are paid API checks.
- Do not mix them into safe checks.
- Do not run them unless the user explicitly asks.

`scripts/check_transcription.ps1` / `tests/smoke_transcription.py` are paid API checks.
- Do not mix them into safe checks.
- Do not run them unless the user explicitly asks.

`scripts/check_outputs.ps1` / `tests/smoke_outputs.py` are output generation checks.
- OpenAI API must not be called.
- Do not mix them into safe checks.
- Do not run large/repeated generations without explicit user request.

`scripts/check_outputs_flags.ps1` / `tests/smoke_outputs_flags.py` are output-flag reflection checks.
- OpenAI/Rakuten/Whisper APIs must not be called.
- They create their own temporary SQLite database plus temporary upload/output directories; do not rewrite them to depend on a fixed real-data Segment ID.
- They verify Word marker (`★引用候補`) and Excel flag columns/values while preserving fixture `Segment.text`.
- They generate temporary Word/Excel artifacts, so keep them out of the default safe gate; run them through the relevant local/output check when needed.

`scripts/check_flags.ps1` / `tests/smoke_flags.py` are segment-flag checks.
- OpenAI/Rakuten/Whisper APIs must not be called.
- They use a self-contained temporary database and temporary runtime directories; they must not mutate the existing research database.
- Keep them out of the default safe gate; run them through the broader local checks when relevant.

`scripts/check_speaker_assignments.ps1` / `tests/smoke_speaker_assignments.py` are speaker-assignment checks.
- OpenAI/Rakuten/Whisper APIs must not be called.
- They use a self-contained temporary database and temporary runtime directories; they must not mutate the existing research database.
- Keep them out of the default safe gate; run them through the broader local checks when relevant.

`scripts/check_all.ps1` is a runner.
- Default: safe check only.
- Paid checks run only when explicit flags are provided.
- Stop at first failed check.

`scripts/run_semantic_analysis.py` / `services/semantic_analysis.py` are semantic analysis tools.
- Do not modify `Segment.text` or raw transcript snapshots.
- Use `--dry-run --no-ai` first when validating clustering behavior.
- Run with `--save` only when the user explicitly requests persistence.
- Fragmentation is derived analysis data; it must not overwrite transcript text.
- Domain glossary normalization must be non-destructive and used only for hinting/search support.
- Show API key status only; never print key values.

## Do not run

- full `compileall`
- all-route checks
- OpenAI API calls
- Whisper transcription
- batch processing
- Word/Excel generation
- large video/audio processing

## AI provider rules

The current provider is OpenAI / GPT API.

- Use `OPENAI_API_KEY`.
- Do not use `ANTHROPIC_API_KEY` unless explicitly requested.
- Do not leave Claude / Anthropic naming in active code, UI, or README unless referring to old history.
- Do not print `.env` values.
- Do not call the OpenAI API unless the user explicitly asks for API testing.
- For structured outputs, preserve evidence fields such as `evidence_quote`.
- Do not fabricate interview statements or analysis findings not supported by source utterances.
- Product-hint enrichment may use Rakuten API, but must not alter raw verbatim transcript text.

## Transcription rules

- Do not run Whisper or process audio/video unless explicitly requested.
- Do not process large media files without first confirming scope.
- For tests, prefer short clips such as `uploads/test_30s.wav`.
- Do not delete source audio/video files unless explicitly requested.

## Commit rules

Before committing:

- Show `git diff --stat`.
- Show `git status`.
- Confirm no secrets or generated files are tracked.
- Commit only the relevant files for the current task.

Recommended commit message for launcher work:

```text
chore: add Windows launcher scripts for local app usage
```
