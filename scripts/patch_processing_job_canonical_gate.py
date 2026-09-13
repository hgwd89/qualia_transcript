from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_block(path: Path, start: str, end: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    path.write_text(text[:start_index] + replacement + text[end_index:], encoding="utf-8")


def replace_exact(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch target not found in {path}: {old[:120]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_processing_jobs_service() -> None:
    path = ROOT / "services" / "processing_jobs.py"
    replace_block(
        path,
        "def create_or_get_active_job(",
        "\ndef _attempt_number(",
        '''def create_or_get_active_job(\n    project_id: int,\n    job_type: str,\n    interview_id: int | None = None,\n    *,\n    question_id: int | None = None,\n):\n    """Compatibility shim around canonical serialized job admission.\n\n    Keep the historical tuple interface for callers that still import this\n    helper, but do not maintain a second admission implementation here.\n    """\n    from services.job_admission import admit_processing_job\n\n    admission = admit_processing_job(\n        project_id,\n        job_type,\n        interview_id,\n        question_id=question_id,\n    )\n    if admission.error:\n        raise ValueError(admission.error)\n    if admission.conflict_job_id is not None:\n        raise ValueError(\n            f"processing job conflicts with active job_id={admission.conflict_job_id}"\n        )\n    if admission.job_id is None:\n        raise RuntimeError("processing job admission returned no job")\n\n    job = db.session.get(ProcessingJob, int(admission.job_id))\n    if job is None:\n        raise RuntimeError("processing job not found after admission")\n    return job, bool(admission.created)\n\n''',
    )
    replace_block(
        path,
        "def retry_failed_job(",
        "\ndef _claim_pending_job(",
        '''def retry_failed_job(job: ProcessingJob) -> ProcessingJob:\n    """Compatibility shim around canonical serialized retry admission."""\n    from services.job_admission import admit_retry_job\n\n    job_id = int(job.id)\n    admission = admit_retry_job(job_id)\n    if admission.conflict_job_id is not None:\n        raise ValueError(\n            f"processing job retry conflicts with active job_id={admission.conflict_job_id}"\n        )\n    if admission.error:\n        raise ValueError(admission.error)\n    if admission.job_id != job_id:\n        raise RuntimeError("processing job retry admission returned an unexpected job")\n    return _refresh_job(job_id)\n\n''',
    )


def patch_processing_jobs_smoke() -> None:
    path = ROOT / "tests" / "smoke_processing_jobs.py"
    replace_exact(
        path,
        "                recorded = _record_worker_launch(launch_job_id, 5555)\n",
        '''                launch_attempt = int(reserved_job.attempt_count or 0)\n                launch_started_at = reserved_job.started_at\n                recorded = _record_worker_launch(\n                    launch_job_id,\n                    5555,\n                    expected_attempt_count=launch_attempt,\n                    expected_started_at=launch_started_at,\n                )\n''',
    )
    replace_exact(
        path,
        "                late_record = _record_worker_launch(launch_job_id, 5555)\n",
        '''                late_record = _record_worker_launch(\n                    launch_job_id,\n                    5555,\n                    expected_attempt_count=launch_attempt,\n                    expected_started_at=launch_started_at,\n                )\n''',
    )
    replace_exact(
        path,
        '''                _, child_reserved = _reserve_worker_launch(child_first_id)\n                child_claim, child_claimed = _claim_pending_job(child_first_id, worker_pid=6666)\n                parent_late_record = _record_worker_launch(child_first_id, 6666)\n''',
        '''                child_reserved_job, child_reserved = _reserve_worker_launch(child_first_id)\n                child_attempt = int(child_reserved_job.attempt_count or 0)\n                child_started_at = child_reserved_job.started_at\n                child_claim, child_claimed = _claim_pending_job(\n                    child_first_id,\n                    worker_pid=6666,\n                    expected_attempt_count=child_attempt,\n                    expected_started_at=child_started_at,\n                )\n                parent_late_record = _record_worker_launch(\n                    child_first_id,\n                    6666,\n                    expected_attempt_count=child_attempt,\n                    expected_started_at=child_started_at,\n                )\n''',
    )
    replace_exact(
        path,
        '''                _, release_reserved = _reserve_worker_launch(release_id)\n                _release_worker_launch_reservation(release_id)\n''',
        '''                release_reserved_job, release_reserved = _reserve_worker_launch(release_id)\n                release_attempt = int(release_reserved_job.attempt_count or 0)\n                release_started_at = release_reserved_job.started_at\n                _release_worker_launch_reservation(\n                    release_id,\n                    expected_attempt_count=release_attempt,\n                    expected_started_at=release_started_at,\n                )\n''',
    )
    replace_exact(
        path,
        '                    first_retry.status == "pending" and second_retry_rejected,\n',
        '''                    first_retry.status == "pending"\n                    and first_retry.started_at is not None\n                    and second_retry_rejected,\n''',
    )
    replace_exact(
        path,
        '''            old_transcribe_launcher = transcribe_routes.launch_job_worker\n            old_analyze_launcher = analyze_routes.launch_job_worker\n            transcribe_routes.launch_job_worker = lambda job_id: 4242\n            analyze_routes.launch_job_worker = lambda job_id: 4343\n''',
        '''            old_transcribe_launcher = transcribe_routes.launch_job_or_preserve_active\n            old_analyze_launcher = analyze_routes.launch_job_or_preserve_active\n            transcribe_routes.launch_job_or_preserve_active = lambda _job: (4242, None)\n            analyze_routes.launch_job_or_preserve_active = lambda _job: (4343, None)\n''',
    )
    replace_exact(
        path,
        '                status_response = client.get(f"/api/processing-jobs/{first_job_id}")\n',
        '''                with app.app_context():\n                    legacy_conflict_rejected = False\n                    try:\n                        create_or_get_active_job(project_id, "map", interview_id)\n                    except ValueError:\n                        legacy_conflict_rejected = True\n                    failures += check(\n                        "legacy create helper delegates canonical conflict policy",\n                        legacy_conflict_rejected,\n                    )\n\n                status_response = client.get(f"/api/processing-jobs/{first_job_id}")\n''',
    )
    replace_exact(
        path,
        '''                transcribe_routes.launch_job_worker = old_transcribe_launcher\n                analyze_routes.launch_job_worker = old_analyze_launcher\n''',
        '''                transcribe_routes.launch_job_or_preserve_active = old_transcribe_launcher\n                analyze_routes.launch_job_or_preserve_active = old_analyze_launcher\n''',
    )


def patch_secondary_analysis_smoke() -> None:
    path = ROOT / "tests" / "smoke_secondary_analysis_jobs.py"
    replace_exact(
        path,
        '''                saved_analyze_launch = analyze_route.launch_job_worker\n                saved_view_launch = analysis_view_route.launch_job_worker\n                analyze_route.launch_job_worker = lambda _job_id: 9101\n                analysis_view_route.launch_job_worker = lambda _job_id: 9102\n''',
        '''                saved_analyze_launch = analyze_route.launch_job_or_preserve_active\n                saved_view_launch = analysis_view_route.launch_job_or_preserve_active\n                analyze_route.launch_job_or_preserve_active = lambda _job: (9101, None)\n                analysis_view_route.launch_job_or_preserve_active = lambda _job: (9102, None)\n''',
    )
    replace_exact(
        path,
        '''                    analyze_route.launch_job_worker = saved_analyze_launch\n                    analysis_view_route.launch_job_worker = saved_view_launch\n''',
        '''                    analyze_route.launch_job_or_preserve_active = saved_analyze_launch\n                    analysis_view_route.launch_job_or_preserve_active = saved_view_launch\n''',
    )


def main() -> None:
    patch_processing_jobs_service()
    patch_processing_jobs_smoke()
    patch_secondary_analysis_smoke()
    print("processing job canonical gate patch applied")


if __name__ == "__main__":
    main()
