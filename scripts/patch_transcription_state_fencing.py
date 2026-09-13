from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_count(path: Path, old: str, new: str, expected: int, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{label}: expected {expected} matches, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    processing = root / "services" / "processing_jobs.py"
    transcription = root / "services" / "transcription.py"
    safe = root / "scripts" / "check_safe.ps1"
    dispatch_doc = root / "docs" / "transcription-dispatch-fencing.md"
    architecture = root / "docs" / "architecture.md"
    testing = root / "docs" / "testing.md"

    replace_once(
        processing,
        '''    update_progress(job, "transcribing")\n    cleanup = discard_incomplete_transcription_segments(media.id)\n\n    tr = Transcription(\n        media_file_id=media.id,\n        whisper_model=get_default_transcription_model(),\n        language="ja",\n        status="pending",\n    )\n    db.session.add(tr)\n    db.session.commit()\n''',
        '''    update_progress(job, "transcribing")\n\n    # Fence cleanup separately. The helper commits internally, so the reservation\n    # must be acquired before it can delete partial Segment rows or supersede an\n    # older incomplete Transcription.\n    begin_job_result_write(job)\n    cleanup = discard_incomplete_transcription_segments(media.id)\n\n    # Fence creation of the next canonical transcription attempt as a second\n    # commit boundary. Recovery may have advanced the attempt after cleanup.\n    begin_job_result_write(job)\n    tr = Transcription(\n        media_file_id=media.id,\n        whisper_model=get_default_transcription_model(),\n        language="ja",\n        status="pending",\n    )\n    db.session.add(tr)\n    db.session.commit()\n''',
        "standalone transcription preflight fencing",
    )

    replace_once(
        transcription,
        '''    tr.status = "running"\n    tr.started_at = datetime.now(timezone.utc)\n    tr.error_message = None\n    db.session.commit()\n\n    media_snapshot = None\n    try:\n''',
        '''    # The running transition is canonical state. Durable callers reserve\n    # the write so recovery cannot supersede this attempt between lease check and\n    # commit. Lease-only direct callers retain the lighter fallback check.\n    if result_write_guard is not None:\n        result_write_guard()\n    elif lease_check is not None:\n        lease_check()\n\n    tr.status = "running"\n    tr.started_at = datetime.now(timezone.utc)\n    tr.error_message = None\n    db.session.commit()\n\n    media_snapshot = None\n    try:\n''',
        "OpenAI running transition",
    )

    replace_once(
        transcription,
        '''            raw_snapshot_path, raw_text_sha256 = _write_raw_transcript_snapshot(\n                transcription_id=transcription_id,\n                interview_id=interview.id,\n                model_name=model_name,\n                language=language,\n                text=raw_text,\n            )\n''',
        '''            # Reserve before immutable evidence publication, not only before\n            # Segment/done writes. Otherwise a recovered stale worker could leave a\n            # fresh source snapshot after losing ownership.\n            if result_write_guard is not None:\n                result_write_guard()\n            elif lease_check is not None:\n                lease_check()\n\n            raw_snapshot_path, raw_text_sha256 = _write_raw_transcript_snapshot(\n                transcription_id=transcription_id,\n                interview_id=interview.id,\n                model_name=model_name,\n                language=language,\n                text=raw_text,\n            )\n''',
        "OpenAI non-chunk evidence reservation",
    )

    replace_once(
        transcription,
        '''            # External API work is complete. Durable callers acquire the result\n            # write reservation before any canonical Segment/done state is written.\n            if result_write_guard is not None:\n                result_write_guard()\n\n            if diarized_segments:\n''',
        '''            if diarized_segments:\n''',
        "remove duplicate non-chunk reservation",
    )

    replace_once(
        transcription,
        '''                    record["error_message"] = err_message\n                    chunk_records.append(record)\n                    manifest_path = _write_chunk_manifest(\n''',
        '''                    record["error_message"] = err_message\n                    chunk_records.append(record)\n                    # Error manifests are immutable evidence too. Do not publish one\n                    # after a newer durable attempt has taken ownership.\n                    if result_write_guard is not None:\n                        result_write_guard()\n                    elif lease_check is not None:\n                        lease_check()\n                    manifest_path = _write_chunk_manifest(\n''',
        "chunk error-manifest reservation",
    )

    replace_once(
        transcription,
        '''        manifest_path = _write_chunk_manifest(\n            transcription_id=transcription_id,\n            interview_id=interview.id,\n            model_name=model_name,\n            language=language,\n            chunk_duration_sec=chunk_duration,\n            overlap_sec=overlap,\n            chunks=chunk_records,\n            status="done",\n        )\n\n        # Partial chunks are auditable/retry-cleanable, but the final canonical\n        # done transition must be fenced against a recovered newer attempt.\n        if result_write_guard is not None:\n            result_write_guard()\n\n        tr.status = "done"\n''',
        '''        # Reserve the durable attempt before publishing the final chunk manifest.\n        # Keep the same reservation through the canonical done-state commit.\n        if result_write_guard is not None:\n            result_write_guard()\n        elif lease_check is not None:\n            lease_check()\n\n        manifest_path = _write_chunk_manifest(\n            transcription_id=transcription_id,\n            interview_id=interview.id,\n            model_name=model_name,\n            language=language,\n            chunk_duration_sec=chunk_duration,\n            overlap_sec=overlap,\n            chunks=chunk_records,\n            status="done",\n        )\n\n        tr.status = "done"\n''',
        "final chunk-manifest reservation",
    )

    replace_once(
        transcription,
        '''    # A recovered worker must not mutate the transcription row or start local\n    # inference once this durable attempt no longer owns the job lease.\n    if lease_check is not None:\n        lease_check()\n\n    tr.status = "running"\n''',
        '''    # The local running transition is canonical state just like the OpenAI\n    # path. Durable callers need the DB write reservation, not a check-then-commit.\n    if result_write_guard is not None:\n        result_write_guard()\n    elif lease_check is not None:\n        lease_check()\n\n    tr.status = "running"\n''',
        "local running transition",
    )

    replace_count(
        transcription,
        '''        # If this exception reflects durable lease loss, the stale worker must\n        # not write even an error transition after recovery has taken ownership.\n        if lease_check is not None:\n            lease_check()\n        tr.status = "error"\n''',
        '''        # Error status is canonical too. Reserve the write so stale recovery\n        # cannot land between ownership validation and this terminal commit.\n        if result_write_guard is not None:\n            result_write_guard()\n        elif lease_check is not None:\n            lease_check()\n        tr.status = "error"\n''',
        2,
        "OpenAI/local error transition",
    )

    replace_once(
        safe,
        '''Write-Host "[PASS] production transcription dispatch fencing smoke checks passed."\n\nWrite-Host "[INFO] Running OpenAI chunk result-write fencing smoke check..."\n''',
        '''Write-Host "[PASS] production transcription dispatch fencing smoke checks passed."\n\nWrite-Host "[INFO] Running transcription lifecycle write-fencing smoke check..."\npython tests/smoke_transcription_state_fencing.py\nif ($LASTEXITCODE -ne 0) {\n    Write-Host "[FAIL] transcription lifecycle write-fencing smoke checks failed (exit code: $LASTEXITCODE)."\n    exit $LASTEXITCODE\n}\nWrite-Host "[PASS] transcription lifecycle write-fencing smoke checks passed."\n\nWrite-Host "[INFO] Running OpenAI chunk result-write fencing smoke check..."\n''',
        "local safe-gate integration",
    )

    replace_once(
        dispatch_doc,
        '''When OpenAI long-audio processing has already committed partial `Segment` rows and then falls back to local Whisper, the dispatcher must acquire the stronger result-write reservation before deleting those partial canonical rows. The local provider must then receive the same lease/result-write callbacks and revalidate ownership before writing replacement evidence or canonical segments.\n\n`tests/smoke_transcription_dispatch_fencing.py` covers callback propagation, cleanup ordering, result-write lease loss, and stale-attempt invalidation without external provider calls.\n''',
        '''When OpenAI long-audio processing has already committed partial `Segment` rows and then falls back to local Whisper, the dispatcher must acquire the stronger result-write reservation before deleting those partial canonical rows. The local provider must then receive the same lease/result-write callbacks and revalidate ownership before writing replacement evidence or canonical segments.\n\nThe durable transcription lifecycle has the same rule at every commit boundary, not only at final success. Standalone transcription preflight reserves cleanup of incomplete attempts and then reacquires the reservation before creating the next `Transcription(status='pending')` row. OpenAI and local Whisper reserve the `running` transition and any `error` transition. OpenAI non-chunk source evidence is reserved before the raw JSON write; long-audio error manifests and the final done manifest are also published only while the attempt owns the result-write reservation. The final manifest reservation remains held through the `done` commit.\n\n`tests/smoke_transcription_dispatch_fencing.py` covers callback propagation, fallback cleanup ordering, result-write lease loss, and stale-attempt invalidation. `tests/smoke_transcription_state_fencing.py` covers preflight cleanup/creation, provider running/error transitions, non-chunk evidence ordering, and chunk manifest ordering without external provider calls.\n''',
        "dispatch lifecycle documentation",
    )

    replace_once(
        architecture,
        '''Canonical result writes have an additional fence. After expensive external work and before changing canonical result rows, `begin_job_result_write()` acquires a database write reservation (`BEGIN IMMEDIATE` on SQLite, row lock on databases that support it) and revalidates the worker's attempt token. That closes the race where stale recovery/retry could supersede a worker between its final lease check and its result commit. Analysis handlers receive this result-write guard before committing `AIAnalysis`; transcription/mapping paths have corresponding cleanup/invalidation helpers in `services/processing_result_guard.py`.\n''',
        '''Canonical result writes have an additional fence. After expensive external work and before changing canonical result rows, `begin_job_result_write()` acquires a database write reservation (`BEGIN IMMEDIATE` on SQLite, row lock on databases that support it) and revalidates the worker's attempt token. That closes the race where stale recovery/retry could supersede a worker between its final lease check and its result commit. Analysis handlers receive this result-write guard before committing `AIAnalysis`; transcription/mapping paths have corresponding cleanup/invalidation helpers in `services/processing_result_guard.py`. Transcription applies the same reservation to preflight cleanup and attempt creation, `running`/`error` status commits, immutable raw-evidence publication, chunk manifests, and final success so no stale worker can mutate state or add evidence after a later attempt takes ownership.\n''',
        "architecture transcription lifecycle fence",
    )

    replace_once(
        testing,
        '''The media-upload/transcription regression also locks the source contract to the implementation:''',
        '''The transcription lifecycle fencing regression uses a temporary SQLite database plus in-process provider/storage stubs. It proves the standalone transcribe handler reserves incomplete-attempt cleanup and pending-row creation as separate commit boundaries, that losing the first reservation performs neither cleanup nor creation, that OpenAI non-chunk evidence is written only after the result reservation, and that OpenAI/local `running` and `error` transitions cannot be committed by a stale worker. Source-order checks additionally keep long-audio error and final manifests behind the durable reservation.\n\nThe media-upload/transcription regression also locks the source contract to the implementation:''',
        "testing transcription lifecycle regression",
    )


if __name__ == "__main__":
    main()
