# Professional readiness contract

This document defines the minimum non-negotiable data-integrity contract for production use.

## Verbatim output

- A delivery verbatim must not depend on question mapping to decide whether a source Segment is included.
- Every Segment belonging to the Interview must appear exactly once in chronological/sequence order.
- Moderator, respondent, observer, and unknown speakers must remain distinguishable.
- Speaker labels are retained alongside human-readable display names where available.
- Segment.text is never modified by output generation.

## Formatted Excel output

- Columns are interview-scoped, not participant-scoped, so repeat interviews with the same participant are preserved independently.
- Speaker role selection uses the human-confirmed `SpeakerAssignment` for the Segment's speaker label when present; `Segment.speaker_role` is the fallback only when no assignment exists.
- A Segment stored as `unknown` but assigned to `respondent` must be eligible for mapped and unclassified formatted output.
- A Segment stored as `respondent` but assigned to a non-respondent role must not be emitted as respondent evidence in the formatted sheet.
- Respondent Segments with no UtteranceMapping are treated as explicit `no_mapping` rows in the `未分類発言` sheet.
- Respondent Segments whose mappings are all unclassified/question-null are retained as `unclassified` rows.
- Flags remain derived metadata; they do not replace or alter the source utterance.

## Segment role mutation boundary

- The Segment must belong to the Interview identified by the route.
- Role updates are one atomic UI selection and require a valid JSON object containing both `speaker_role` and `participant_id`; malformed, empty, or incomplete payloads are rejected before mutation.
- `speaker_role` must be one of the supported canonical roles.
- `participant_id`, when non-null, must belong to the same Project as the Interview. Explicit null clears the participant association.
- Failed validation must not mutate role, participant association, or `Segment.text`.

## Required local regression check

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_professional_integrity.ps1
```

The broader professional smoke uses a temporary SQLite database and temporary output directory. It must not call OpenAI or Whisper. It verifies full chronological Word output, repeated-interview Excel preservation, zero-mapping preservation, role-update scope guards, and Segment.text immutability.

The required safe gate additionally runs `tests/smoke_role_assignment_integrity.py` without generating deliverable files. It verifies malformed/no-op role-update rejection and effective-role selection from `SpeakerAssignment` for formatted-output mapping eligibility.
