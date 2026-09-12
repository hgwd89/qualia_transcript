# Qualia Transcript Architecture

## Purpose

Qualia Transcript is a local Flask application for qualitative interview operations. It supports project setup, participant management, interview-flow design, media upload, transcription, speaker assignment, segmentation, AI-assisted analysis, semantic analysis, human review workflows, and Word or Excel output generation.

The core architecture rule is separation of raw source data from derived analysis data. Raw transcript snapshots, uploaded media, and `Segment.text` are source records. Mapping, flags, speaker assignments, semantic clusters, AI analyses, review state, source evidence links, quote candidates, and generated files are derived records.

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
- Human approval and evidence resolution through `services/analysis_review.py` and the analysis review routes/UI.
- Semantic analysis through `services/semantic_analysis.py` and `scripts/run_semantic_analysis.py`.
- Integrated analysis dry-run through `services/integrated_analysis.py` and the interview preview route.
- Word and Excel output generation through `services/report_verbatim.py`, `services/report_formatted.py`, `services/report_analysis.py`, `services/report_approved_analysis.py`, and `routes/outputs.py`.

## Processing Flow

1. Media is uploaded and associated with an interview.
2. Transcription creates transcript records and raw transcript snapshots.
3. Segments are stored as source utterance units.
4. Speaker assignment classifies labels as respondent, moderator, observer, or other roles.
5. Mapping links respondent utterances to interview-flow questions.
6. Segment flags mark favorite, quote, exclude, and needs-review states without changing segment text.
7. AI analysis and semantic analysis create derived `AIAnalysis` records or dry-run results. Newly generated `AIAnalysis` rows are not formally approved by default.
8. A human reviewer may approve, reject, or return an AI analysis to draft. Approval resolves each finding's `evidence_quote` against respondent `Segment` rows inside the analysis scope and persists `source_segment_ids`; unresolved findings block approval.
9. Integrated no-ai analysis previews existing derived data without saving.
10. Word and Excel output services generate files and register `GeneratedFile` rows. Formal AI-analysis XLSX output selects `review_status=approved` rows only and carries evidence quotes plus source segment IDs.

## Raw Data vs Derived Data

Raw data includes uploaded media, raw transcript snapshots, `Transcription` rows, and `Segment.text`.

Derived data includes speaker assignments, utterance mappings, segment flags, semantic clusters, AI analyses, AI review state, source-segment evidence links, quote candidates, review decisions, and generated output files.

Derived data may reference raw data by IDs and source quotes, but must not overwrite raw data. Any code path that changes `Segment.text` must be treated as high risk and requires explicit approval.

## AI Analysis Review Contract

`AIAnalysis.review_status` is one of `draft`, `approved`, or `rejected`. Existing and newly generated analyses default to `draft`; model generation never implies human approval.

Approval requires a non-empty `findings` array. Every finding must have a non-empty `evidence_quote`, and that quote must resolve to one or more respondent `Segment` rows scoped to the analysis project and, when present, the analysis interview. Participant codes and question codes further constrain resolution when those fields are supplied by the finding.

Successful approval writes `source_segment_ids` into each finding in `content_json` and records `reviewed_at`. Failed evidence resolution leaves the analysis unapproved. The formal analysis workbook reads approved rows only and refuses approved findings that lack evidence quotes or source segment IDs.

This review layer is derived-data governance. It must not rewrite `Segment.text` or raw transcript snapshots.

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
- `AIAnalysis`: derived structured analysis payload plus human review state (`review_status`, `review_note`, `reviewed_at`).
- `ProcessingJob`: durable background-work record for transcription, mapping, analysis, and project pipelines; question-scoped jobs may reference `InterviewFlowQuestion`.
- `GeneratedFile`: generated output metadata, including `approved_analysis` XLSX outputs.
- `AppSetting`: local application settings. Secret values are storage records and must be consumed through the secret-store service rather than read as plaintext directly.

## Core Routes

- `routes/projects.py`: project views and actions.
- `routes/participants.py`: participant views and actions.
- `routes/flows.py`: interview-flow editing.
- `routes/interviews.py`: interview detail, segments, flags, speaker assignment, unclassified review, and integrated preview.
- `routes/transcribe.py`: transcription actions.
- `routes/analyze.py`: API-backed mapping and AI analysis actions.
- `routes/analysis_view.py`: project-level analysis views plus human AI-analysis review UI/API.
- `routes/outputs.py`: Word, Excel, flat-analysis, and approved-analysis output generation.
- `routes/settings.py`: local settings UI.

## Core Services

- `services/transcription.py`: OpenAI or Whisper transcription and raw transcript snapshot writing.
- `services/mapper.py`: OpenAI-backed mapping of respondent utterances to questions.
- `services/analyzer.py`: OpenAI-backed interview, question, cross-participant, and integrated AI analysis.
- `services/analysis_review.py`: human review transitions and evidence-quote → respondent source-segment resolution.
- `services/semantic_analysis.py`: semantic clustering and dry-run/no-ai support.
- `services/integrated_analysis.py`: no-ai integrated analysis assembly from existing local data.
- `services/report_verbatim.py`: Word verbatim report generation.
- `services/report_formatted.py`: Excel formatted sheet generation.
- `services/report_analysis.py`: flat utterance/mapping analysis CSV/XLSX generation.
- `services/report_approved_analysis.py`: formal XLSX generation from approved `AIAnalysis` rows only, with separate analysis-summary and evidence sheets.
- `services/secret_store.py`: protected application-secret storage/read boundary.
- `services/product_hint.py`, `services/domain_glossary.py`, `services/fragmentation.py`: derived text-analysis helpers that must not alter source transcript text.

## Secret Storage Contract

On Windows, `services/secret_store.py` protects configured secret settings with current-user DPAPI and stores them with the `dpapi:v1:` marker. Consumers must call `get_secret_setting()` rather than read secret `AppSetting` values directly. This applies to chat/transcription clients, product-hint provider credentials, and semantic embedding clients.

Environment values remain the fallback when no database secret is configured. Existing plaintext database secrets are migrated to DPAPI during Windows application startup. Migration failure is non-destructive: startup continues, and legacy plaintext remains readable until migration can succeed. Non-Windows environments do not rewrite database secrets into a weaker plaintext representation and continue to rely on supported fallbacks.

The settings UI exposes only configured/not-configured state for password fields; decrypted secret values are not rendered back into HTML.

## Checks and Tests

Smoke tests live in `tests/smoke_*.py`. PowerShell wrappers live in `scripts/check_*.ps1`.

The default aggregate runner is `scripts/check_all.ps1`. With no flags, it runs the CI-safe smoke check only. `scripts/check_all.ps1 -AllLocal` runs the broader non-paid local suite using temporary fixtures/directories where applicable, including the AI-analysis review/approved-export smoke. Paid/API checks remain opt-in and are not part of the safe or `-AllLocal` path.

Existing research data is protected by a separate manual `local-data-integrity` check. That check opens the SQLite database read-only, verifies key table relationships, reports source-segment fingerprints and analysis counts, and can compare minimum counts and raw-transcript file hashes against an optional baseline. It is deliberately not a CI-required check because CI does not have the local research dataset.

## Generated Files and Non-Git Data

The following data must remain untracked:

- `.env`
- `instance/`
- `uploads/`
- `outputs/`
- `outputs/raw_transcripts/`
- `*.db`
- `*.db-journal`
- `*.sqlite3-journal`
- `logs/*.log`
- generated Word, Excel, CSV, and transcript files
- virtual environments and caches

## External API Boundaries

OpenAI API usage appears in transcription, mapping, analyzer, and semantic-analysis paths. These must not be run as part of default safe checks or `-AllLocal`. OpenAI credentials must be obtained through the protected secret-store boundary, with the environment used only as fallback when no database secret is configured.

Whisper usage appears in transcription paths. It must not be run unless transcription has been explicitly requested.

Human AI-analysis review, source-evidence resolution, approved-analysis export, no-ai integrated analysis, and preview checks are local operations and do not require OpenAI or Whisper.

## Unconfirmed Items

- The exact production database lifecycle is not documented here beyond the local Flask/SQLAlchemy behavior observed in `app.py`.
- The behavior of every semantic-analysis mode with respect to OpenAI embeddings must be checked before running it outside `--dry-run --no-ai`.
- Large media performance, long-running transcription behavior, and bulk output-generation limits are not validated by the safe checks.
- Evidence resolution currently depends on textual quote matching plus available participant/question scope; it is conservative and may require manual correction when an AI quote paraphrases rather than reproduces the source text.
