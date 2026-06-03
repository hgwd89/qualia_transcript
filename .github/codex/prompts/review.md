# Codex PR Review Prompt

Review this PR strictly.

Focus on:

- Whether the implementation satisfies the issue acceptance criteria.
- Whether the change scope is too broad.
- Whether `AGENTS.md`, `CLAUDE.md`, `docs/architecture.md`, `docs/testing.md`, and `docs/github-operations.md` remain consistent.
- Whether docs mention commands or files that do not exist.
- Whether tests were removed, weakened, or made less meaningful.
- Whether DB schema, API format, save format, or output columns changed without approval.
- Whether paid/API or Whisper checks were mixed into safe checks.
- Whether `.env`, API keys, real data, generated outputs, logs, or DB files are included.
- Whether large refactors or UI changes were introduced without need.
- Whether branch protection and manual GitHub settings are documented.
- Whether validation commands and results are clearly reported.
- Whether unconfirmed items are clearly reported.

High-priority risks:

- Raw transcript snapshots modified or deleted.
- `Segment.text` modified.
- Interviewer, moderator, observer, or unknown speaker text treated as respondent evidence without explicit logic.
- Evidence fields such as `evidence_quote`, `source_segment_ids`, `source_segment_quotes`, or `quote_ids` removed or fabricated.
- OpenAI or Whisper invoked without explicit approval.
- Generated files, database files, logs, uploads, outputs, or secrets staged.

If issues exist, identify the smallest corrective change. Do not recommend weakening tests or changing product requirements to make the PR pass.
