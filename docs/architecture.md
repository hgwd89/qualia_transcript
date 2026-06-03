# Qualia Transcript Architecture

## Purpose

Qualia Transcript is a local Flask application for qualitative interview operations. It supports project setup, participant management, interview-flow design, media upload, transcription, speaker assignment, segmentation, AI-assisted analysis, semantic analysis, review workflows, and Word or Excel output generation.

The core architecture rule is separation of raw source data from derived analysis data. Raw transcript snapshots, uploaded media, and `Segment.text` are source records. Mapping, flags, speaker assignments, semantic clusters, AI analyses, quote candidates, and generated files are derived records.

## Technology Stack

- Python and Flask provide the local web application.
- Flask-SQLAlchemy provides ORM access.
- OpenAI is used by paid/API analysis, mapping, semantic, and transcription paths when explicitly run.
- faster-whisper is available for local transcription when explicitly run.
- python-docx generates Word reports.
- openpyxl generates Excel reports.
- PowerShell scripts in `scripts/` wrap smoke checks for Windows local use.

## Main Features

- Project management through `models/project.py` and `routes/projects.py`.
- Participant management through `models/participant.py` and `routes/participants.py`.
- Interview-flow management through `models/interview_flow.py` and `routes/flows.py`.
- Media and transcription management through `models/interview.py`, `routes/transcribe.py`, and `services/transcription.py`.
- Transcript segment storage through `models/segment.py`.
- Question mapping through `UtteranceMapping` and `services/mapper.py`.
- Segment flags through `models/segment_flag.py` and flag API routes in `routes/interviews.py`.
- Speaker assignments through `models/speaker_assignment.py` and speaker routes in `routes/interviews.py`.
- AI analysis through `models/analysis.py`, `services/analyzer.py`, `routes/analyze.py`, and `routes/analysis_view.py`.
- Semantic analysis through `services/semantic_analysis.py` and `scripts/run_semantic_analysis.py`.
- Integrated analysis dry-run through `services/integrated_analysis.py` and the interview preview route.
- Word and Excel output generation through `services/report_verbatim.py`, `services/report_formatted.py`, `services/report_analysis.py`, and `routes/outputs.py`.

## Processing Flow

1. Media is uploaded and associated with an interview.
2. Transcription creates transcript records and raw transcript snapshots.
3. Segments are stored as source utterance units.
4. Speaker assignment classifies labels as respondent, moderator, observer, or other roles.
5. Mapping links respondent utterances to interview-flow questions.
6. Segment flags mark favorite, quote, exclude, and needs-review states without changing segment text.
7. AI analysis and semantic analysis create derived `AIAnalysis` records or dry-run results.
8. Integrated no-ai analysis previews existing derived data without saving.
9. Word and Excel output services generate files and register `GeneratedFile` rows.

## Raw Data vs Derived Data

Raw data includes uploaded media, raw transcript snapshots, `Transcription` rows, and `Segment.text`.

Derived data includes speaker assignments, utterance mappings, segment flags, semantic clusters, AI analyses, quote candidates, review decisions, and generated output files.

Derived data may reference raw data by IDs and source quotes, but must not overwrite raw data. Any code path that changes `Segment.text` must be treated as high risk and requires explicit approval.

## Core Models

- `Project`: research project container.
- `Participant`: respondent metadata and participant code.
- `InterviewFlow`, `InterviewFlowSection`, `InterviewFlowQuestion`: interview guide structure.
- `Interview`: interview session and participant linkage.
- `MediaFile`: uploaded media metadata.
- `Transcription`: transcription status and transcript metadata.
- `Segment`: source utterance text and timing metadata.
- `UtteranceMapping`: derived link from segment to question.
- `SegmentFlag`: derived flags for review and output behavior.
- `SpeakerAssignment`: derived mapping from speaker label to role and participant.
- `AIAnalysis`: derived structured analysis payload and status.
- `GeneratedFile`: generated output metadata.
- `AppSetting`: local application settings.

## Core Routes

- `routes/projects.py`: project views and actions.
- `routes/participants.py`: participant views and actions.
- `routes/flows.py`: interview-flow editing.
- `routes/interviews.py`: interview detail, segments, flags, speaker assignment, unclassified review, and integrated preview.
- `routes/transcribe.py`: transcription actions.
- `routes/analyze.py`: API-backed mapping and AI analysis actions.
- `routes/analysis_view.py`: project-level analysis views.
- `routes/outputs.py`: Word, Excel, and analysis output generation.
- `routes/settings.py`: local settings UI.

## Core Services

- `services/transcription.py`: OpenAI or Whisper transcription and raw transcript snapshot writing.
- `services/mapper.py`: OpenAI-backed mapping of respondent utterances to questions.
- `services/analyzer.py`: OpenAI-backed interview, question, cross-participant, and integrated AI analysis.
- `services/semantic_analysis.py`: semantic clustering and dry-run/no-ai support.
- `services/integrated_analysis.py`: no-ai integrated analysis assembly from existing local data.
- `services/report_verbatim.py`: Word verbatim report generation.
- `services/report_formatted.py`: Excel formatted sheet generation.
- `services/report_analysis.py`: analysis CSV/XLSX generation.
- `services/product_hint.py`, `services/domain_glossary.py`, `services/fragmentation.py`: derived text-analysis helpers that must not alter source transcript text.

## Checks and Tests

Smoke tests live in `tests/smoke_*.py`. PowerShell wrappers live in `scripts/check_*.ps1`.

The default aggregate runner on current `master` is `scripts/check_all.ps1`. With no flags, it runs the safe smoke check only. Paid/API and output checks are opt-in.

Some local checks on current `master` still use reversible writes or fixed local fixtures. Treat their behavior according to `docs/testing.md` and the current script implementation before running them.

## Generated Files and Non-Git Data

The following data must remain untracked:

- `.env`
- `uploads/`
- `outputs/`
- `outputs/raw_transcripts/`
- `*.db`
- `logs/*.log`
- generated Word, Excel, CSV, and transcript files
- virtual environments and caches

## External API Boundaries

OpenAI API usage appears in transcription, mapping, analyzer, and semantic-analysis paths. These must not be run as part of default safe checks.

Whisper usage appears in transcription paths. It must not be run unless transcription has been explicitly requested.

No-ai integrated analysis and preview checks are intended to validate local data assembly without OpenAI, Whisper, or database persistence.

## Unconfirmed Items

- The exact production database lifecycle is not documented here beyond the local Flask/SQLAlchemy behavior observed in `app.py`.
- The behavior of every semantic-analysis mode with respect to OpenAI embeddings must be checked before running it outside `--dry-run --no-ai`.
- The current `master` check suite differs from active feature branches and PRs that may add self-contained `-AllLocal` behavior.
- Large media performance, long-running transcription behavior, and bulk output-generation limits are not validated by the safe checks.
