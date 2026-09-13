# Qualia Transcript Testing and Check Guide

## Purpose

This guide defines the required safe test gate and the optional local/provider-backed checks. `AGENTS.md` remains the top-level rule.

## Required safe gate

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

The same required category is run by `.github/workflows/safe-check.yml` as the Windows `safe-smoke` job and by `scripts/check_all.ps1` with no flags. CI also runs focused managed-storage regressions directly on both Windows and Ubuntu. The Ubuntu `posix-backup-snapshot` job exercises backup snapshot fencing, managed-storage ancestry-replacement read/write behavior, write identity, and the DB commit-window replacement race on a POSIX filesystem rather than leaving those paths Windows-only or source-assertion-only.

Safe checks use temporary fixtures where data or storage is needed. They must not depend on existing research-data IDs, must not change `Segment.text` or raw transcript snapshots, and must leave the repository's real runtime data directories unchanged.

The required `safe-smoke` gate currently covers:

- baseline application/model/route initialization
- launcher runtime configuration, including `0.0.0.0 -> 127.0.0.1`, IPv6 unspecified `:: -> ::1`, RFC-compliant bracketed IPv6 URLs, and unchanged hostname/IPv4 URL behavior
- generated-file integrity, including project-scoped path enforcement, UUID-isolated internal storage for same-name outputs, rollback isolation, bounded storage basenames for long display filenames, rejection of linked/reparse project storage paths, ancestry-pinned exclusive creation for every DOCX/XLSX/CSV report writer, exact-generation re-pinning through the DB commit window, post-commit namespace verification and compensating row deletion if POSIX pathname replacement occurs during commit, handle-backed download bytes, `Content-Length`/`Last-Modified`/ETag preservation, byte-range `206` behavior, and legacy nullable download-name fallback
- media-upload integrity, including rejection of linked/reparse interview storage paths, ancestry-pinned exclusive creation of uploaded bytes, exact-generation re-pinning through DB commit, post-commit namespace verification with compensating `MediaFile` deletion, and transcription input snapshotting from an ancestry-pinned managed handle; the required regressions verify exact uploaded bytes, a private snapshot pathname outside `UPLOAD_DIR`, stability after the managed ID pathname is replaced, deterministic snapshot cleanup, and source-level exclusion of pathname-based `FileStorage.save()` writes
- managed-storage path guards that reject symlinks and Windows junction/reparse entries below configured output/upload roots; both reads and new-file writes use ancestry-pinned acquisition rather than check-then-reopen pathnames. POSIX descends through descriptor-relative `open/stat` plus `O_NOFOLLOW` and creates with `O_CREAT|O_EXCL`; Windows retains read-share-only component handles opened with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse handles, denies both write and delete sharing until final acquisition, and creates destinations with `CREATE_NEW`
- backup/restore integrity hardening: the backup tool resolves the same SQLite file as Flask, validates and restores the same staged archive bytes, honors the manifest-declared database member, removes a newly created DB when restore rolls back, preserves a raw damaged DB copy only under explicit recovery acknowledgement, keeps backup destinations/artifacts current-user-only with POSIX modes or an explicit Windows current-user SID ACL, rejects linked/junction/reparse entries inside upload/output backup trees rather than following them, copies managed backup regular files through descriptor identity fencing and rejects unsupported special entries instead of silently omitting them, uses descriptor-relative `open/stat` beneath pinned POSIX directory descriptors so ancestor pathname replacement cannot redirect backup or rollback reads outside the acquired tree, uses size/mtime/ctime in regular-file generation checks to reject unlink/recreate reuse that reuses a device/inode pair, preserves POSIX extended attributes for rollback files/directories and fails before live mutation if required rollback metadata cannot be captured or recreated, builds pre-restore rollback trees only from regular files/directories, preserves rollback directory metadata, builds each new backup under an unpublished owner-private same-filesystem partial path, validates those exact bytes, flushes the validated partial before rename, publishes with a same-filesystem atomic rename, then completes the platform durability barrier before reporting success (POSIX parent-directory fsync; Windows write-through rename plus final-file flush), cleans both partial and prematurely exposed official names on normal validation/publish/durability failure, and enforces the exclusive maintenance boundary inside the backup/restore service API itself whenever any configured live recovery-set target is involved
- runtime maintenance exclusion: canonical `create_app()` launches retain a shared runtime slot for the complete process lifetime before DB/schema/storage initialization, including alternate Flask/factory launch paths; detached durable workers, semantic-analysis CLI, operational read-only readers, and explicit processing-job recovery writes share the same runtime slot range; backup and applied restore hold the exclusive maintenance lock for the whole operation. Same-process reentry is reference-counted only for the same lock mode; exceptions raised inside nested runtime/maintenance contexts must propagate unchanged, nested exits must unwind the reference count without suppressing the protected exception, and the underlying OS lock must remain held until the outermost context exits. Read-only job inspection, integrated-analysis dry-run, the production-readiness wrappers, all three direct readiness audit entry points (`audit_production_readiness.py`, `_v2.py`, and `_project.py`), and both wrapper/direct manual local-data integrity entry points therefore refuse to start while maintenance is replacing or snapshotting the live recovery set
- durable processing-job admission integrity: `BEGIN IMMEDIATE` admission remains the concurrency boundary and also validates the authoritative project/interview/question ownership contract before a durable row is created or retried; each job type enforces its required/forbidden scope IDs, question analysis requires the question to belong to the interview's assigned flow, malformed or cross-project failed jobs cannot be requeued, and an AST-based safe check prevents operational Python code from reintroducing the legacy `create_or_get_active_job()` bypass
- project deletion lifecycle, including durable-job serialization, pre-commit quarantine of existing output/upload numeric ID entries and captured processing-job logs, deletion refusal when quarantine cannot be established, quarantine restoration on DB rollback, non-recursive linked-path handling, immediate SQLite Project/ProcessingJob ID-reuse safety after post-commit cleanup failure, mandatory byte-for-byte retention of raw transcript snapshots, and transactionally persisted non-FK raw-snapshot provenance tombstones containing a stable owner token, full-file SHA-256, original project/interview/transcription IDs, and generation timestamps before deletable owner rows disappear
- participant/interview-flow project boundaries and delete guards
- participant identity integrity: `(project_id, participant_code)` is unique for new schemas, legacy SQLite installs receive non-destructive duplicate-write triggers, automatic `Pxx` allocation never reuses a deleted count slot, create/edit conflicts fail without mutating the existing participant, historical duplicates become readiness blockers, and project-scoped readiness isolates participant rows to the selected project
- interview-creation scope validation
- segment-role normalization/update integrity: canonical `moderator` handling, malformed or empty JSON rejection without mutation, and partial role updates that preserve an existing participant unless `participant_id` is explicitly supplied
- SQLite foreign-key enforcement
- legacy `processing_jobs.question_id` compatibility guards: insert/update triggers must reject orphan question references even when `PRAGMA foreign_keys=OFF`
- readiness detection of declared SQLite FK violations, explicit `ProcessingJob.question_id` orphans, and semantic durable-job scope violations whose referenced IDs still exist but belong to the wrong project/flow or violate the job type's required/forbidden interview/question shape
- readiness traceability regressions: question mappings are rejected when an interview has no assigned flow, every approved `source_segment_id` must individually support the finding's `evidence_quote`, and a retained raw transcript snapshot can satisfy a completed transcription only when its `transcription_id`, `interview_id`, and `created_at_utc` match the current `started_at`–`completed_at` generation and its filename is not already tombstoned to a deleted stable owner. Reusing the same SQLite project/interview/transcription integer IDs—even with identical generation timestamps—must not let a predecessor snapshot mask a missing replacement snapshot
- project-scoped readiness isolation: `--project-id` excludes another project's workflow/content and ProcessingJob defects while keeping shared database integrity and recovery-set checks global; participant rows are also shadowed by project-scoped TEMP VIEWs, and semantic scope blockers, question-orphan blockers, and active-job warnings retain exact per-project counts plus bounded project samples before global diagnostic caps are applied, so a later project cannot disappear behind another project's first 200/100 rows; the source SQLite file remains byte-for-byte unchanged
- resumed project-analysis UI recovery: the shared job script must wrap `pollAnalysisJob` before the page's resume handler runs and must re-enable the associated analysis button before propagating a polling error
- Windows DPAPI secret-store behavior

The linked managed-storage regression uses only temporary output/upload roots. It proves that ordinary managed IDs remain usable and static symlink/junction/reparse entries are rejected. On POSIX-capable runners it replaces an ID-directory pathname after the original parent descriptor has been acquired and verifies both that a final read still comes from the pinned original directory and that a new write is created beneath that pinned directory rather than the replacement target; the Ubuntu workflow runs this regression directly. Source-level assertions keep the Windows contract explicit: component handles are opened with `FILE_FLAG_OPEN_REPARSE_POINT`, reparse handles are rejected, both `FILE_SHARE_WRITE` and `FILE_SHARE_DELETE` are deliberately omitted while the chain is retained, and final creation uses `CREATE_NEW`. The same source contract requires every generated-output writer plus media upload to use the managed create boundary and forbids the former `save(target.full_path)`/`open(target.full_path)` patterns. The generated-file integrity regression exercises the actual Flask download route and verifies normal bytes, length/mtime/ETag metadata, byte-range `206` responses, and fallback naming for legacy rows whose `original_filename` is null.

The managed write commit-window regression uses SQLAlchemy's `before_commit` hook to race the public managed directory only after the exact written generation has already been re-pinned. On POSIX, where renaming an open directory is allowed, it proves both generated outputs and uploads detect the post-guard namespace replacement after commit, compensate the just-created database row, delete only the pinned predecessor generation, and leave the replacement decoy untouched. On Windows, it proves the retained ancestor/final handles deny delete sharing so the same rename is rejected while the DB commit proceeds normally.

The media-upload/transcription regression also locks the source contract to the implementation: both OpenAI and local-Whisper paths must call `create_media_read_snapshot(media)`, neither may call `get_media_full_path(media)`, and both must close the snapshot. This keeps pathname-based decoder/provider consumers isolated from later replacement of the managed upload path without invoking a provider during the safe check.

The backup service-boundary regression uses only temporary database/upload/output/backup/lock paths. It proves that direct `services.local_backup.create_backup()` and applied `restore_backup()` calls cannot bypass an active runtime holder when pointed at the configured live recovery set, that a refused restore leaves the live database unchanged, and that a linked/reparse entry in a managed backup tree is rejected before it can be copied into an archive. The managed backup collector regression proves that ordinary regular files still stage correctly, pathname replacement between inspection and open is rejected by descriptor identity fencing, linked/reparse entries are rejected, and unsupported special entries are not silently omitted. The backup/restore hardening regression additionally proves that restore rollback snapshots refuse linked/reparse entries before replacing the current database or managed trees, preserve directory metadata, fence regular-file copying to an already-opened descriptor whose identity matches the checked path, and reject unsupported special entries rather than silently omitting them. The snapshot-fencing regression additionally proves that unlink/recreate replacement cannot be hidden merely by device/inode reuse; on POSIX it replaces an ancestor pathname after the original directory has been acquired and proves both rollback snapshotting and backup collection continue from the pinned directory object rather than the attacker-controlled replacement, and it verifies `user.*` extended attributes on rollback files/directories when the temporary filesystem supports them. The atomic-publish regression proves that no official backup filename appears before full validation, that validated bytes are forced to stable storage before rename, that publication completes the correct platform durability barrier before success is reported, that Windows creates the partial beneath a current-user-only ACL boundary, and that validation, rename, or post-rename durability failure leaves neither a false completed backup nor a stale partial artifact. Custom all-temporary fixture targets remain available without acquiring the live maintenance lock.

The raw-snapshot provenance regression uses only a temporary database and temporary output tree. It proves project deletion leaves source JSON bytes unchanged, records both raw text snapshots and chunk manifests under one stable owner token with exact full-file hashes and original owner IDs, rejects a tombstoned predecessor after actual SQLite project/interview/transcription ID reuse even when the replacement reuses the same generation timestamps, accepts only a new non-tombstoned replacement snapshot, and rolls back a staged tombstone when the surrounding project deletion transaction fails.

The participant-code identity regression uses a temporary legacy-style SQLite database whose pre-existing `participants` table has duplicate historical codes and no composite unique constraint. It proves startup preserves those rows, installs forward INSERT/UPDATE guards, rejects new duplicates, keeps create/edit conflicts non-destructive, allocates `Pxx` monotonically after deletion, blocks historical duplicates in global readiness, and keeps the blocker plus participant counts isolated to the owning project in project-scoped readiness. The detailed contract is in `docs/participant-identity.md`.

The runtime-maintenance regression also exercises same-process nested locking directly. It proves that same-mode nesting is allowed, mode switching is rejected, an exception raised inside a nested maintenance context is not swallowed by context-manager cleanup, all nested reference counts unwind on that exception, and the lock can be reacquired afterward. This prevents a service-level backup/restore failure from being converted into normal control flow when a CLI already owns the same maintenance lock.

The app-factory runtime regression uses only temporary database/upload/output/backup/lock paths. It proves that a canonical factory launch keeps maintenance blocked after `create_app()` returns, that the lock disappears when the process exits, and that an exclusive maintenance holder causes factory startup to fail before database or managed-storage initialization.

The Windows-only DPAPI regression reports a skip/pass on non-Windows systems. On Windows it validates local protection/migration behavior, verifies that covered application consumers receive decrypted usable settings without contacting an external service, and verifies settings-form atomicity: if a later secret field cannot be protected, earlier secret and non-secret field changes from the same POST are rolled back together rather than partially committed.

## Broader non-paid local suite

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -AllLocal
```

This adds segment flags, speaker assignments, output flags, integrated no-AI checks, CLI guards, AI-analysis review/export, and integrated preview checks. These checks must use temporary fixtures/directories where applicable.

## Manual local data integrity

Use only for read-only inspection of the existing local research database and transcript snapshots:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_local_data_integrity.ps1
```

This check is manual-only and must not become a required CI gate. It opens SQLite read-only and checks important table relationships, source fingerprints, analysis counts, and snapshot hashes without modifying the source dataset. Both the PowerShell wrapper and direct `python tests/smoke_local_data_integrity.py` entry point hold the shared `reader` runtime lock, so neither can inspect the live DB while backup/applied-restore maintenance owns the exclusive recovery-set boundary.

## Integrated analysis no-AI checks

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_cli_guards.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_integrated_analysis_preview_ui.ps1
```

Expected contract: no external request, no save for dry-run preview, no `Segment.text` change, and no raw transcript change. Direct integrated-analysis CLI execution holds the shared `reader` runtime lock while it reads the live database.

## AI analysis review/export

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_analysis_review.ps1
```

Use temporary fixtures and output paths. Approval must require resolvable respondent evidence and formal exports must remain approved-only.

## Segment flags

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_flags.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

Flags remain derived data separate from `Segment.text`; self-contained output checks write only to temporary directories.

## Speaker assignments

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_speaker_assignments.ps1
```

Use temporary fixtures. Duplicate upserts must not create duplicate assignment rows.

## Output generation

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_outputs.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

Generated artifacts must stay out of Git and output generation must not modify source transcripts. The output check also runs a self-contained formatted-sheet regression proving that human `SpeakerAssignment` roles override stale `Segment.speaker_role` values for both mapped and unclassified respondent rows.

## Provider-backed checks

Run only when explicitly requested:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_mapping.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_analysis.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_transcription.ps1
```

These are not part of safe-smoke or the normal CI path.

## Semantic analysis

Start with dry-run/no-AI:

```powershell
python scripts/run_semantic_analysis.py --interview-id <id> --dry-run --no-ai
```

`Segment.text` and raw snapshots must remain unchanged. Fragmentation and normalization are derived data. Provider-backed semantic execution must use the application's protected settings boundary. The semantic CLI holds the shared runtime lock even for dry-run because application initialization may perform idempotent schema/secret migration; it therefore refuses to start while backup/applied-restore maintenance owns the exclusive lock.

## PR preflight

Before proposing completion:

```powershell
git status --short
git diff --stat
git diff --check
```

Run checks relevant to the changed files. For broad test-infrastructure changes, run `scripts/check_all.ps1 -AllLocal` when the local dependencies are available. Generated/private runtime files must not be staged.

For GitHub review and branch-protection guidance, also read `docs/github-operations.md`.