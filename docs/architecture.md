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
- `services/job_admission.py`: authoritative durable-job project/interview/question ownership and job-type scope validation, plus serialized admission/retry and conflict handling.
- `services/processing_jobs.py`: worker launch, lease ownership, progress, execution, and terminal state handling.
- `services/processing_result_guard.py`: crash-window reuse and canonical result-write cleanup/fencing helpers.
- `services/storage_paths.py`: shared output/upload path validation, including symlink and Windows reparse/junction rejection below managed roots, plus OS-level ancestry-pinned reads for generated-output downloads.
- `services/local_backup.py`: authoritative verified local backup/restore boundary for the application SQLite database, uploads, and outputs; it owns manifest/hash validation, protected backup publication, linked/reparse tree rejection, snapshot ancestry fencing, live recovery-set maintenance exclusion, staged restore, and rollback metadata preservation.
- `services/runtime_lock.py`: cross-platform shared/exclusive process-lifetime lock; app, detached workers, and operational readers use shared runtime slots, canonical app-factory launches retain a process-lifetime slot automatically, and backup/applied restore use exclusive maintenance mode.
- `services/project_deletion.py`: serialized project deletion with pre-commit output/upload/job-log quarantine, rollback restoration, and post-commit cleanup of bound entries.
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

## Durable Processing Job Contract

Long-running transcription, mapping, participant analysis, question analysis, cross-participant analysis, integrated analysis, and project-pipeline work use `ProcessingJob` rather than executing the expensive operation inside the initiating request. The API route admits a durable job, launches a worker, returns a job ID, and the UI polls the job status endpoint. A browser reload can therefore resume observation of an existing `pending` or `running` job instead of starting the work again.

`services/job_admission.py` is the authoritative boundary for both admission concurrency and durable-job scope. On SQLite it acquires `BEGIN IMMEDIATE` before validating ownership or deciding whether a job may be admitted, so project deletion and job creation observe one serialized database snapshot. The service verifies that the project exists, that required/forbidden `interview_id` and `question_id` fields match the job type, that referenced interviews and questions belong to the job project, and, for `analyze_question`, that the question belongs to the interview's assigned flow. Failed-job retry revalidates the stored scope under the same reservation; malformed or cross-project legacy rows remain failed rather than being requeued. Operational routes/services/scripts must use this boundary rather than the legacy `create_or_get_active_job()` helper, and the required safe smoke checks that invariant through Python AST inspection.

After scope validation, same-scope active work is reused where appropriate, incompatible active work is rejected, and a new `pending` row is created only while the write reservation is held. Retry admission uses the same serialization. This prevents two requests from independently observing an empty slot and both creating conflicting durable jobs.

`services/processing_jobs.py` treats `status='running'` plus `attempt_count` as the worker lease. Claiming a pending job increments the attempt token. Progress updates and terminal writes are conditional on the same immutable attempt number; if stale recovery or retry has moved ownership to a later attempt, the older worker raises `JobLeaseLost` rather than continuing to publish progress or success.

Canonical result writes have an additional fence. After expensive external work and before changing canonical result rows, `begin_job_result_write()` acquires a database write reservation (`BEGIN IMMEDIATE` on SQLite, row lock on databases that support it) and revalidates the worker's attempt token. That closes the race where stale recovery/retry could supersede a worker between its final lease check and its result commit. Analysis handlers receive this result-write guard before committing `AIAnalysis`; transcription/mapping paths have corresponding cleanup/invalidation helpers in `services/processing_result_guard.py`.

Crash-window idempotency is explicit. `services/processing_result_guard.py` can detect mapping or analysis results that were already committed after the durable job was created but before the worker managed to mark the job `succeeded`. A retry reuses that committed result instead of duplicating canonical analysis rows. Stale transcription attempts can be invalidated and their partial derived segments removed without rewriting immutable raw transcript snapshots.

Worker and UI failure are recoverable states rather than UI locks. Failed jobs can be retried through durable admission; stale jobs are recovered before conflicting admission/deletion decisions; and resumed project-analysis polling must restore its associated button when polling fails so the user can retry instead of remaining permanently disabled.

## Legacy ProcessingJob Question Integrity Contract

New SQLite databases receive the model-declared foreign key from `processing_jobs.question_id` to `interview_flow_questions.id`. Older installations are different: `question_id` was historically added with `ALTER TABLE`, and SQLite cannot attach a foreign-key constraint to that existing column in place.

The compatibility strategy is deliberately non-destructive. Startup keeps the existing durable `processing_jobs` table and rows unchanged, ensures the `question_id` column exists, and installs two SQLite triggers for inserts and question-ID updates. Those triggers reject a non-null `question_id` when the referenced interview-flow question does not exist. They remain effective even if `PRAGMA foreign_keys` is disabled, so legacy tables receive a database-level guard without a table rebuild or history rewrite.

Existing invalid values are never silently repaired, nulled, or deleted. `audit_production_readiness_v2.py` checks normal `PRAGMA foreign_key_check`, explicit `ProcessingJob.question_id` orphans, and semantic durable-job scope using the real `main.*` ownership graph. Professional readiness is blocked when a historical row violates the job type's required/forbidden interview/question shape, references an interview or question owned by another project, cannot resolve a question back to a flow/project, or stores an `analyze_question` question outside the interview's assigned flow. Project-scoped readiness computes those ownership violations globally first and then filters the resulting job list/count to the selected project, so another project's durable-job defect does not block a project-only delivery while shared database-level integrity checks remain global. This separates forward admission enforcement from non-destructive historical-data detection.

## Backup and Restore Contract

The application SQLite database has one canonical absolute path under `instance/`. `config.DATABASE_URI`, backup creation, readiness tooling, and restore must refer to that same file; legacy explicit relative `sqlite:///...` URIs are resolved using Flask's instance-directory rule rather than the repository root.

`services/local_backup.py` creates a recovery set containing a SQLite snapshot plus uploads and outputs, records exact size/SHA-256 metadata in a manifest, and validates archive membership, hashes, and SQLite integrity before a backup is accepted. Backup destination directories and artifacts are current-user-only on both supported platform families: POSIX uses owner-only modes, while Windows rebuilds a protected DACL so inherited and unrelated explicit ACEs are removed and only the current logon SID receives FullControl. The destination ACL is established before an unpublished partial archive is created, preventing an early-crash window in which sensitive backup bytes could inherit a broader pre-existing ACL.

Backup publication is validation- and durability-gated. The archive is built under an owner-private dot-prefixed `.partial` name on the destination filesystem, those exact bytes are fully validated, and the validated file is flushed before the same-filesystem atomic rename. POSIX then fsyncs the destination directory; Windows uses `MoveFileExW` with `MOVEFILE_WRITE_THROUGH` and flushes the final file handle. Only after that platform durability barrier succeeds is the official `qualia_backup_....zip` considered published. Normal validation, rename, or post-rename durability failures remove both unpublished partials and any official name exposed before the success barrier.

Managed-tree snapshotting rejects symlinks, Windows junctions/reparse points, and unsupported special entries rather than following or silently omitting them. On POSIX, backup collection and restore rollback traversal pin each directory with descriptor-relative `open/stat` plus `O_DIRECTORY|O_NOFOLLOW`, so replacing an ancestor pathname after acquisition cannot redirect later reads outside the acquired tree. Regular-file snapshot checks include device/inode plus mode, size, mtime, and ctime around the open/copy boundary, closing unlink/recreate cases where a filesystem reuses the same inode number. Rollback snapshots preserve file/directory modes, times, and POSIX extended attributes; if required rollback metadata cannot be captured or recreated safely, restore fails before live managed trees are mutated.

The service boundary, not only the command-line wrappers, owns maintenance exclusion. A direct `create_backup()` call that touches any configured live database/upload/output target acquires the exclusive `maintenance` lock before snapshotting. An applied `restore_backup()` does the same before replacing live targets. CLI wrappers may already hold the same maintenance lock; same-mode nesting is intentional and preserves one uninterrupted exclusion boundary. Calls whose database, uploads, and outputs are all explicit non-live fixture paths remain lock-free so safe tests do not contend on the real runtime lock. Validation-only restore does not mutate the live recovery set and does not require maintenance exclusion.

Applied restore is deliberately staged. The source ZIP is first copied into a private temporary location, that staged copy is fully validated, and extraction/restoration uses those same bytes so a subsequently replaced source archive cannot change the recovery payload after validation. The database member used for restore is the manifest-declared `database_archive_path`; validation requires that member to be present in the declared file set.

Restore snapshots the pre-existing database/uploads/outputs for rollback before replacement. If the target database did not exist before restore and a later step fails, the newly created database is removed rather than left as a partial recovery. When the existing database is damaged enough that a normal verified pre-restore backup cannot be produced, recovery requires an explicit acknowledgement and preserves the original database bytes as a separate unvalidated current-user-only copy before replacement.

Every application process that creates the Flask app against the canonical configured local database retains a shared runtime slot before `db.create_all()`, migrations, or managed-directory creation. This covers `python app.py` as well as alternate factory launchers such as Flask CLI or another WSGI-style caller of `create_app()`. The hold remains for the process lifetime rather than ending when the factory returns. Temporary/test databases do not receive this automatic retained hold, preventing safe fixtures from leaving live lock files or open handles in temporary directories.

Detached durable workers and operational readers share the same runtime-lock slot range. Backup and applied restore acquire that range in exclusive `maintenance` mode before reading or replacing the live recovery set and hold it until the full operation completes. Shared live holders can coexist, but maintenance cannot overlap any of them; conversely, canonical app-factory startup, workers, and readers fail while maintenance owns the lock. Normal interpreter shutdown releases retained process holds, and OS process teardown is the final stale-lock safety net after crashes or forced termination.

The lock is non-blocking and uses `msvcrt.locking` on Windows and `fcntl.lockf` on POSIX. The SQLite `BEGIN EXCLUSIVE` probe during applied restore remains a secondary defense; the process-lifetime runtime lock is the lifecycle boundary that prevents database/file-tree snapshots or replacements from racing with live readers or writers.

## Managed Storage Path Contract

Generated output and uploaded media are stored under configured managed roots. `GeneratedFile.stored_path` is project-scoped (`<project_id>/<uuid>.<ext>`), and `MediaFile.stored_path` is interview-scoped (`<interview_id>/<uuid>.<ext>`). User-facing filenames are metadata only and do not determine internal path components.

`services/storage_paths.py` owns path validation for these output/upload stores. Absolute paths and `..` traversal are rejected, the resolved target must remain inside the configured root, and every existing path component below that root must be a normal entry rather than a symlink, Windows junction, or other reparse point. ID directories are checked before and after creation. This prevents a linked `outputs/<project_id>` or `uploads/<interview_id>` directory from redirecting a generated file or source-media upload into another managed ID directory.

Generated-output downloads do not use a check-then-reopen pathname sequence. `open_managed_file_for_read()` resolves the operator-controlled managed root and then acquires the requested file while pinning every ancestor for the complete acquisition. On POSIX it descends through descriptor-relative `open/stat` with `O_DIRECTORY|O_NOFOLLOW`; the final regular file is opened relative to the pinned parent and checked against pre/open/post generation metadata. On Windows it opens each component with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse handles, and keeps the component handle chain read-share-only: both `FILE_SHARE_WRITE` and `FILE_SHARE_DELETE` are omitted. That blocks write-side reparse mutation as well as rename, deletion, and replacement while acquisition is in progress. The returned stream is bound to that exact file object even if mutable pathnames change afterward.

The download route streams from the already-opened handle and reconstructs HTTP metadata from its `fstat()` result. `Content-Length`, `Last-Modified`, ETag conditional handling, and byte-range `206` responses are preserved without reopening the pathname. Legacy `GeneratedFile` rows whose nullable `original_filename` is unset fall back to the stored basename. Resolver-based registration and other reads still reject linked/reparse entries, but they do not claim the stronger ancestry-pinned guarantee unless they use the open-handle boundary explicitly. Filesystem and database operations still cannot form one transaction; UUID-only basenames and rollback cleanup remain the collision/partial-write controls around that boundary.

## Project Deletion Contract

Project deletion is coordinated with durable processing jobs. After stale-job recovery, `services/project_deletion.py` starts a serialized database write transaction before checking active jobs. On SQLite it uses `BEGIN IMMEDIATE`, matching job admission's write reservation, so a new job cannot pass admission between the active-job check and the project deletion commit. Active `pending` or `running` jobs block deletion.

Before the project graph is committed as deleted, every pre-existing numeric project-output directory, interview-upload directory, and captured `processing_job_<id>.log` file is renamed inside its own managed root to a unique quarantine path. This rename binds later filesystem cleanup to the exact entry observed before deletion. It applies to normal directories/files as well as symlinks, Windows junctions, and other reparse entries; link targets are never traversed. If any required quarantine rename fails, the database deletion is aborted. If a later database commit fails, all successfully quarantined entries are renamed back to their original reusable names before the error is returned.

After a successful database commit, cleanup removes only the bound quarantine entries. It never recursively deletes an output/upload numeric ID path or a `processing_job_<id>.log` path after commit, because SQLite may already have reused that integer ID for a replacement record/job. If a reusable path reappears after staging, it is left untouched and reported as a cleanup warning. Failure to remove a quarantine entry is also a cleanup warning; stale data remains under a unique non-reusable name and therefore cannot be inherited or deleted through SQLite ID reuse.

Raw transcript snapshots under `outputs/raw_transcripts/` are immutable source snapshots and are outside generic project-deletion cleanup. Deleting a project does not enumerate, unlink, rewrite, or otherwise mutate those snapshots, even after the corresponding transcription rows are removed. Any raw-snapshot purge must be a separate, explicit user-requested operation rather than an implicit side effect of project deletion.

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

- The migration framework remains intentionally lightweight and SQLite-specific rather than a general schema-migration system.
- The behavior of every semantic-analysis mode with respect to OpenAI embeddings must be checked before running it outside `--dry-run --no-ai`.
- Large media performance, long-running transcription behavior, and bulk output-generation limits are not validated by the safe checks.
- Evidence resolution currently depends on textual quote matching plus available participant/question scope; it is conservative and may require manual correction when an AI quote paraphrases rather than reproduces the source text.