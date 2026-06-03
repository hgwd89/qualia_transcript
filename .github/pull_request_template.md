## Summary

- 

## Acceptance Criteria

- [ ] Requirements are traceable to an issue, PR description, or repository doc.
- [ ] The change stays within the intended scope.
- [ ] Raw transcript snapshots are not modified.
- [ ] `Segment.text` is not modified unless explicitly approved.
- [ ] Uploaded media, generated outputs, database files, logs, and `.env` are not committed.

## Safety Classification

Select all that apply:

- [ ] Documentation only
- [ ] Safe local check, no external API
- [ ] Reversible local DB write
- [ ] Word/Excel/CSV output generation
- [ ] OpenAI API check
- [ ] Whisper/transcription check
- [ ] Database schema or migration change

## Validation

Commands run:

```text

```

Results:

```text

```

Required GitHub check:

- [ ] `safe-smoke` passed, or the failure is explained and unrelated to this PR.

## Raw Data and Evidence Safety

- [ ] Analysis results remain derived data.
- [ ] Evidence fields such as `evidence_quote`, `source_segment_ids`, `source_segment_quotes`, or `quote_ids` are preserved when relevant.
- [ ] Moderator, observer, interviewer, and unknown speaker turns are not silently treated as respondent evidence.
- [ ] Quote candidates remain tied to source segment text or reviewed text.

## External APIs and Generated Files

- [ ] No OpenAI API call was made, or the call was explicitly requested and documented.
- [ ] No Whisper run was made, or the run was explicitly requested and documented.
- [ ] No generated Word/Excel/CSV files are staged.
- [ ] No secrets or `.env` values are printed in logs or PR text.

## Reviewer Notes

- 
