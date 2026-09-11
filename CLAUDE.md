# CLAUDE.md

## Purpose

This file is a companion guide for Claude Code work on Qualia Transcript.

`AGENTS.md` is the highest-priority repository rule. Read it before making any change. If this file and `AGENTS.md` conflict, follow `AGENTS.md`.

## Operating Rules

- Inspect the relevant files before editing.
- Explain the intended impact and risk before making broad changes.
- Keep changes small and reviewable.
- Do not infer product requirements from chat history unless they are copied into the repository, an issue, or a PR description.
- Do not implement unclear behavior by guessing. Stop and ask for a concrete requirement when the choice changes data safety, external API usage, or output semantics.
- Preserve the separation between raw data and derived analysis data.
- Do not modify raw transcript snapshots, uploaded media, generated outputs, existing database files, or `.env`.
- Do not modify `Segment.text` for analysis, labeling, glossary normalization, fragmentation, product-hint enrichment, or output generation.
- Do not call OpenAI APIs unless the user explicitly requests an API check.
- Do not run Whisper unless the user explicitly requests transcription.
- Do not run Word or Excel output generation unless the user explicitly requests output checks.
- Do not mix paid/API checks into safe checks.

## Reading Map

- Transcription and raw transcript snapshots: `routes/transcribe.py`, `services/transcription.py`, `models/interview.py`.
- Segments and question mapping: `models/segment.py`, `services/mapper.py`, `routes/analyze.py`.
- Speaker assignment: `models/speaker_assignment.py`, `routes/interviews.py`, `tests/smoke_speaker_assignments.py`.
- Segment flags: `models/segment_flag.py`, `routes/interviews.py`, `tests/smoke_flags.py`.
- AI analysis: `models/analysis.py`, `services/analyzer.py`, `routes/analyze.py`, `routes/analysis_view.py`.
- Semantic analysis: `services/semantic_analysis.py`, `scripts/run_semantic_analysis.py`, `tests/smoke_integrated_analysis_noai.py`.
- Integrated analysis dry-run: `services/integrated_analysis.py`, `routes/interviews.py`, `templates/interviews/integrated_analysis_preview.html`.
- Word and Excel outputs: `services/report_verbatim.py`, `services/report_formatted.py`, `services/report_analysis.py`, `routes/outputs.py`.

## Safe Work Pattern

- Start with `git status --short`.
- Read `AGENTS.md`, this file, and the relevant architecture or testing docs.
- Identify whether the task is documentation-only, safe local validation, output generation, paid/API validation, or data mutation.
- For safe changes, run only the relevant safe checks.
- For paid/API checks, require explicit user approval and report that the check will call the external provider.
- For output checks, make clear that Word or Excel files may be generated.
- Before commit or PR, confirm that generated files, database files, logs, uploads, outputs, raw transcript snapshots, and secrets are not staged.

## Quality Gates

- Analysis must keep evidence fields such as `evidence_quote`, `source_segment_ids`, `source_segment_quotes`, and `quote_ids` when those fields are part of the relevant contract.
- Interviewer, moderator, observer, and unknown speaker turns must not be silently treated as respondent evidence.
- If speaker assignment is missing, do not guess a respondent unless the code path explicitly defines that behavior.
- Quote candidates and official quote outputs must remain tied to original segment text or reviewed text.
- Semantic analysis should be checked with dry-run and no-ai mode before any persistence or API-backed analysis is attempted.

## Documentation Duties

- Update `docs/architecture.md` when model, route, service, or data-flow responsibilities change.
- Update `docs/testing.md` when a check script, smoke test, safe check, paid check, or output-generation check changes.
- Update `docs/github-operations.md` when branch protection, PR review, CODEOWNERS, or GitHub Actions guidance changes.
- Keep PR descriptions explicit about external API usage, database writes, generated outputs, and raw transcript or `Segment.text` safety.
