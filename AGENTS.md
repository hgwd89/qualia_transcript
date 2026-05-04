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
- If logs are needed for debugging, show only relevant non-secret excerpts.

## Local launcher task rules

For Windows local launcher work, only edit:

- `start_app.ps1`
- `stop_app.ps1`
- `open_app.ps1`
- `README_LOCAL.md`
- `.gitignore`
- `logs/.gitkeep`
- `AGENTS.md`

### Requirements

- `start_app.ps1` must start `python app.py` in the background.
- It must log to `logs/flask_out.log` and `logs/flask_err.log`.
- It must avoid double-starting if port 5000 is already serving HTTP 200.
- It must wait for readiness with a retry loop: max 30 seconds, 1-second interval, success on HTTP 200.
- It must not kill unrelated processes.
- `stop_app.ps1` may stop only the Qualia Transcript Flask process using port 5000.
- `open_app.ps1` should only open `http://127.0.0.1:5000/`.

## Lightweight verification

For launcher-only changes, run only:

```powershell
.\start_app.ps1
Invoke-WebRequest http://127.0.0.1:5000/ -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:5000/settings -UseBasicParsing
.\open_app.ps1
.\stop_app.ps1
git status
git diff --stat
```

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
