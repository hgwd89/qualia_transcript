# Codex Text Analysis Guardrail Prompt

Harden text-analysis guardrails with the smallest safe change.

Inspect:

- `models/segment.py`
- `models/segment_flag.py`
- `models/speaker_assignment.py`
- `models/analysis.py`
- `services/analyzer.py`
- `services/semantic_analysis.py`
- `services/integrated_analysis.py`
- `routes/analyze.py`
- `routes/analysis_view.py`
- relevant smoke tests and check scripts

Guardrails to preserve:

- `Segment.text` is source data and must not be overwritten.
- Raw transcript snapshots must not be modified or deleted.
- Analysis, fragmentation, clustering, product hints, quote candidates, and output flags are derived data.
- Evidence fields must stay traceable to source segments.
- Speaker roles must not be guessed into respondent evidence when assignment is missing or unknown.
- AI-generated wording must not become official quote text without source trace.
- OpenAI and Whisper must stay out of safe checks.

When adding tests:

- Use temporary DB and temporary directories when possible.
- Avoid external APIs.
- Avoid existing local fixture IDs.
- Keep assertions strict.

Required report:

- Guardrail strengthened.
- Files changed.
- Checks run.
- Remaining risks.
