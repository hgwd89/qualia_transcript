# Codex CI Fix Prompt

Fix the failing check with the smallest safe change.

Rules:

- Read `AGENTS.md` first.
- Do not call OpenAI or Whisper.
- Do not modify `Segment.text`.
- Do not modify raw transcript snapshots, uploads, outputs, existing DB files, logs, or `.env`.
- Do not weaken assertions to make CI pass.
- Do not remove tests unless the test is demonstrably obsolete and the PR explains why.
- Do not mix paid/API checks into safe checks.
- Keep the fix limited to the failing check path.

Required report:

- Failing command and failure summary.
- Root cause.
- Files changed.
- Validation command and result.
- Any remaining risk or unconfirmed item.
