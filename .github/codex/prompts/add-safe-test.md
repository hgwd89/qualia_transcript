# Codex Add Safe Test Prompt

Add or improve a safe local test.

Safe means:

- No OpenAI API calls.
- No Whisper execution.
- No generated Word/Excel/CSV files unless the test is explicitly an output-generation check.
- No dependency on existing local DB fixture IDs.
- No mutation of `Segment.text`.
- No mutation of raw transcript snapshots, uploads, outputs, logs, or `.env`.

Prefer:

- Temporary SQLite database.
- Temporary upload/output directories.
- Explicit fixture creation.
- Assertions that preserve source evidence and derived-data separation.

Do not:

- Weaken expectations.
- Hide failures with broad skips.
- Add duplicate tests when an existing smoke can be extended.
- Stage generated files.

Required report:

- What behavior is now covered.
- Why the test is safe.
- Validation command and result.
