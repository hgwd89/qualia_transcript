import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_exact(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"patch target not found in {path}: {old[:220]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_model() -> None:
    path = ROOT / "models" / "processing_job.py"
    replace_exact(
        path,
        '    # transcribe / map / analyze / analyze_question / analyze_cross / analyze_integrated / project_pipeline\n',
        '    # transcribe / map / analyze / analyze_semantic / analyze_question / analyze_cross / analyze_integrated / project_pipeline\n',
    )
    replace_exact(
        path,
        '    progress_json = db.Column(db.Text)\n    result_json = db.Column(db.Text)\n',
        '    progress_json = db.Column(db.Text)\n    request_json = db.Column(db.Text)\n    result_json = db.Column(db.Text)\n',
    )
    replace_exact(
        path,
        '            "progress": self._json_value(self.progress_json),\n            "result": self._json_value(self.result_json),\n',
        '            "progress": self._json_value(self.progress_json),\n            "request": self._json_value(self.request_json),\n            "result": self._json_value(self.result_json),\n',
    )


def patch_app_migration() -> None:
    path = ROOT / "app.py"
    replace_exact(
        path,
        '''            if "question_id" not in job_cols:\n                db.session.execute(text(\n                    "ALTER TABLE processing_jobs ADD COLUMN question_id INTEGER"\n                ))\n                db.session.commit()\n            _install_processing_job_question_guards()\n''',
        '''            if "question_id" not in job_cols:\n                db.session.execute(text(\n                    "ALTER TABLE processing_jobs ADD COLUMN question_id INTEGER"\n                ))\n                db.session.commit()\n            if "request_json" not in job_cols:\n                db.session.execute(text(\n                    "ALTER TABLE processing_jobs ADD COLUMN request_json TEXT"\n                ))\n                db.session.commit()\n            _install_processing_job_question_guards()\n''',
    )


def patch_admission() -> None:
    path = ROOT / "services" / "job_admission.py"
    replace_exact(path, 'from dataclasses import dataclass\n\nfrom sqlalchemy import text\n', 'from dataclasses import dataclass\nimport json\n\nfrom sqlalchemy import text\n')
    marker = '''\ndef _same_scope(\n    job: ProcessingJob,\n'''
    helpers = '''\ndef _normalize_request_payload(job_type: str, request_payload: dict | None) -> dict | None:\n    if job_type != "analyze_semantic":\n        if request_payload not in (None, {}):\n            raise ValueError(f"{job_type} processing job does not accept request payload")\n        return None\n\n    raw = request_payload or {}\n    if not isinstance(raw, dict):\n        raise ValueError("analyze_semantic request payload must be an object")\n    unknown = set(raw) - {"max_segments", "no_ai"}\n    if unknown:\n        raise ValueError(f"unsupported analyze_semantic request field(s): {', '.join(sorted(unknown))}")\n\n    max_segments = raw.get("max_segments")\n    if max_segments is not None:\n        try:\n            max_segments = int(max_segments)\n        except (TypeError, ValueError) as exc:\n            raise ValueError("analyze_semantic max_segments must be a positive integer or null") from exc\n        if max_segments <= 0:\n            raise ValueError("analyze_semantic max_segments must be a positive integer or null")\n\n    no_ai = raw.get("no_ai", False)\n    if not isinstance(no_ai, bool):\n        raise ValueError("analyze_semantic no_ai must be boolean")\n    return {"max_segments": max_segments, "no_ai": no_ai}\n\n\ndef _canonical_request_json(payload: dict | None) -> str | None:\n    if payload is None:\n        return None\n    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))\n\n\ndef _job_request_json(job: ProcessingJob) -> str | None:\n    if job.request_json:\n        try:\n            payload = json.loads(job.request_json)\n        except (TypeError, json.JSONDecodeError):\n            return str(job.request_json)\n        return _canonical_request_json(payload)\n    if job.job_type == "analyze_semantic":\n        return _canonical_request_json({"max_segments": None, "no_ai": False})\n    return None\n\n\ndef _same_request(job: ProcessingJob, request_json: str | None) -> bool:\n    return _job_request_json(job) == request_json\n\n'''
    replace_exact(path, marker, helpers + marker)
    replace_exact(
        path,
        '''def admit_processing_job(\n    project_id: int,\n    job_type: str,\n    interview_id: int | None = None,\n    *,\n    question_id: int | None = None,\n) -> JobAdmission:\n''',
        '''def admit_processing_job(\n    project_id: int,\n    job_type: str,\n    interview_id: int | None = None,\n    *,\n    question_id: int | None = None,\n    request_payload: dict | None = None,\n) -> JobAdmission:\n''',
    )
    replace_exact(
        path,
        '''    project_id = int(project_id)\n    interview_id = int(interview_id) if interview_id is not None else None\n    question_id = int(question_id) if question_id is not None else None\n\n    recover_stale_jobs(project_id=project_id)\n''',
        '''    project_id = int(project_id)\n    interview_id = int(interview_id) if interview_id is not None else None\n    question_id = int(question_id) if question_id is not None else None\n    normalized_request = _normalize_request_payload(job_type, request_payload)\n    request_json = _canonical_request_json(normalized_request)\n\n    recover_stale_jobs(project_id=project_id)\n''',
    )
    replace_exact(
        path,
        '''        for job in active:\n            if _same_scope(job, job_type, interview_id, question_id):\n                db.session.commit()\n                return JobAdmission(job_id=job.id, created=False)\n''',
        '''        for job in active:\n            if _same_scope(job, job_type, interview_id, question_id):\n                db.session.commit()\n                if _same_request(job, request_json):\n                    return JobAdmission(job_id=job.id, created=False)\n                return JobAdmission(job_id=job.id, conflict_job_id=job.id)\n''',
    )
    replace_exact(
        path,
        '''            status="pending",\n            progress_json=_json_dump({"stage": "queued"}),\n        )\n''',
        '''            status="pending",\n            progress_json=_json_dump({"stage": "queued"}),\n            request_json=request_json,\n        )\n''',
    )


def patch_processing_jobs() -> None:
    path = ROOT / "services" / "processing_jobs.py"
    marker = '''\ndef _perform_semantic_analysis(\n    job: ProcessingJob,\n'''
    helper = '''\ndef _semantic_job_request(job: ProcessingJob) -> dict:\n    raw = None\n    if job.request_json:\n        try:\n            raw = json.loads(job.request_json)\n        except (TypeError, json.JSONDecodeError) as exc:\n            raise ValueError(f"invalid semantic job request_json for job_id={job.id}") from exc\n    if raw is None:\n        raw = {"max_segments": None, "no_ai": False}\n    if not isinstance(raw, dict):\n        raise ValueError(f"invalid semantic job request payload for job_id={job.id}")\n\n    max_segments = raw.get("max_segments")\n    if max_segments is not None:\n        max_segments = int(max_segments)\n        if max_segments <= 0:\n            raise ValueError(f"invalid semantic max_segments for job_id={job.id}")\n    no_ai = raw.get("no_ai", False)\n    if not isinstance(no_ai, bool):\n        raise ValueError(f"invalid semantic no_ai for job_id={job.id}")\n    return {"max_segments": max_segments, "no_ai": no_ai}\n\n'''
    replace_exact(path, marker, helper + marker)
    start = path.read_text(encoding="utf-8").index("def _perform_semantic_analysis(\n")
    end = path.read_text(encoding="utf-8").index("\n\ndef _perform_project_pipeline", start)
    text = path.read_text(encoding="utf-8")
    replacement = '''def _perform_semantic_analysis(job: ProcessingJob) -> dict:\n    from models.interview import Interview\n    from services.processing_result_guard import find_completed_analysis_for_scope\n    from services.semantic_analysis import run_semantic_cluster_analysis\n\n    interview = db.session.get(Interview, job.interview_id)\n    if not interview or interview.project_id != job.project_id:\n        raise ValueError("interview not found in job project")\n\n    existing = find_completed_analysis_for_scope(job, "semantic_clusters")\n    if existing:\n        update_progress(job, "analyzing_semantic", analysis_id=existing.id, already_done=True)\n        return {"analysis_id": existing.id, "already_done": True}\n\n    request_payload = _semantic_job_request(job)\n    update_progress(job, "analyzing_semantic", **request_payload)\n    result = run_semantic_cluster_analysis(\n        interview.id,\n        save=True,\n        max_segments=request_payload["max_segments"],\n        no_ai=request_payload["no_ai"],\n        result_write_guard=lambda: begin_job_result_write(job),\n    )\n    if not result.get("ok", False):\n        diagnostic = {\n            key: result.get(key)\n            for key in (\n                "reason",\n                "candidate_segment_count",\n                "fragment_count",\n                "excluded_count",\n                "excluded_counts",\n            )\n            if key in result\n        }\n        raise RuntimeError(\n            "semantic analysis produced no savable result: "\n            + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)\n        )\n\n    analysis_id = result.get("saved_analysis_id")\n    if not analysis_id:\n        raise RuntimeError("semantic analysis completed without a saved AIAnalysis row")\n    return {\n        "analysis_id": int(analysis_id),\n        "cluster_count": int(result.get("cluster_count") or 0),\n        "embedding_api_call_count": int(result.get("embedding_api_call_count") or 0),\n        "summary_api_call_count": int(result.get("summary_api_call_count") or 0),\n    }\n'''
    path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


def patch_cli() -> None:
    path = ROOT / "scripts" / "run_semantic_analysis.py"
    replace_exact(
        path,
        '''                    admission = admit_processing_job(\n                        int(interview.project_id),\n                        "analyze_semantic",\n                        int(interview.id),\n                    )\n''',
        '''                    admission = admit_processing_job(\n                        int(interview.project_id),\n                        "analyze_semantic",\n                        int(interview.id),\n                        request_payload={\n                            "max_segments": args.max_segments,\n                            "no_ai": bool(args.no_ai),\n                        },\n                    )\n''',
    )
    replace_exact(
        path,
        '''                    elif not admission.created:\n                        job = db.session.get(ProcessingJob, int(admission.job_id))\n                        result = {\n                            "ok": True,\n                            "deduplicated": True,\n                            "job": job.to_dict() if job else None,\n                        }\n                    else:\n                        completed = execute_job(\n                            int(admission.job_id),\n                            handlers={\n                                "analyze_semantic": lambda job: _perform_semantic_analysis(\n                                    job,\n                                    max_segments=args.max_segments,\n                                    no_ai=bool(args.no_ai),\n                                )\n                            },\n                            worker_pid=os.getpid(),\n                        )\n''',
        '''                    elif not admission.created:\n                        job = db.session.get(ProcessingJob, int(admission.job_id))\n                        result = {\n                            "ok": False,\n                            "in_progress": True,\n                            "error_type": "JobAlreadyActive",\n                            "error_message": "same-scope semantic analysis is still pending or running",\n                            "job": job.to_dict() if job else None,\n                        }\n                    else:\n                        completed = execute_job(\n                            int(admission.job_id),\n                            worker_pid=os.getpid(),\n                        )\n''',
    )
    replace_exact(
        path,
        '''    if not result.get("ok", False):\n        return 1\n    return 0\n''',
        '''    if result.get("in_progress"):\n        return 2\n    if not result.get("ok", False):\n        return 1\n    return 0\n''',
    )
    replace_exact(
        path,
        'from services.processing_jobs import _perform_semantic_analysis, execute_job\n',
        'from services.processing_jobs import execute_job\n',
    )


def patch_readiness() -> None:
    path = ROOT / "scripts" / "audit_production_readiness_v2.py"
    replace_exact(
        path,
        '    "analyze": (True, False),\n    "analyze_question": (True, True),\n',
        '    "analyze": (True, False),\n    "analyze_semantic": (True, False),\n    "analyze_question": (True, True),\n',
    )


def patch_docs() -> None:
    path = ROOT / "docs" / "architecture.md"
    replace_exact(
        path,
        '- `ProcessingJob`: durable background-work record for transcription, mapping, analysis, and project pipelines; question-scoped jobs may reference `InterviewFlowQuestion`.\n',
        '- `ProcessingJob`: durable background-work record for transcription, mapping, semantic analysis, other AI analysis, and project pipelines; question-scoped jobs may reference `InterviewFlowQuestion`, while `request_json` preserves immutable execution options needed by retries.\n',
    )
    replace_exact(
        path,
        'Long-running transcription, mapping, participant analysis, question analysis, cross-participant analysis, integrated analysis, and project-pipeline work use `ProcessingJob` rather than executing the expensive operation inside the initiating request. The API route admits a durable job, launches a worker, returns a job ID, and the UI polls the job status endpoint. A browser reload can therefore resume observation of an existing `pending` or `running` job instead of starting the work again.\n',
        'Long-running transcription, mapping, semantic analysis saves, participant analysis, question analysis, cross-participant analysis, integrated analysis, and project-pipeline work use `ProcessingJob` rather than publishing expensive results outside the durable-job contract. The API/CLI entry point admits a durable job, a worker claims an attempt, and callers observe the durable state instead of treating an active duplicate as completed. Semantic dry-runs remain non-persistent; `run_semantic_analysis.py --save` is a durable-job producer.\n',
    )
    replace_exact(
        path,
        'After scope validation, same-scope active work is reused where appropriate, incompatible active work is rejected, and a new `pending` row is created only while the write reservation is held. Retry admission uses the same serialization. This prevents two requests from independently observing an empty slot and both creating conflicting durable jobs.\n',
        'After scope validation, same-scope active work is reused only when its durable request parameters are identical, incompatible active work is rejected, and a new `pending` row is created only while the write reservation is held. Retry admission uses the same serialization and retains `request_json`, so semantic `max_segments` and `no_ai` cannot silently change on retry or detached-worker execution. This prevents two requests from independently observing an empty slot and both creating conflicting durable jobs.\n',
    )

    path = ROOT / "docs" / "testing.md"
    needle = '- durable processing-job admission integrity: `BEGIN IMMEDIATE` admission remains the concurrency boundary and also validates the authoritative project/interview/question ownership contract before a durable row is created or retried; each job type enforces its required/forbidden scope IDs, question analysis requires the question to belong to the interview\'s assigned flow, integrated analysis revalidates its single-flow research scope inside that serialized admission boundary, malformed or cross-project failed jobs cannot be requeued, and an AST-based safe check prevents operational Python code from reintroducing the legacy `create_or_get_active_job()` bypass\n'
    addition = needle + '- semantic durable-job integrity: persisted semantic analysis uses `analyze_semantic`, requires a result-write lease before `AIAnalysis` commit, preserves `max_segments`/`no_ai` in durable `request_json` across retry and detached execution, rejects mismatched same-scope requests, reports an already-active same-scope CLI job as in-progress rather than success, preserves no-fragments diagnostics, supports current-attempt crash-window result recovery, and runs providerless on both Windows and Ubuntu in `.github/workflows/processing-jobs.yml`\n'
    replace_exact(path, needle, addition)


def main() -> None:
    patch_model()
    patch_app_migration()
    patch_admission()
    patch_processing_jobs()
    patch_cli()
    patch_readiness()
    patch_docs()
    print("semantic review fixes applied")


if __name__ == "__main__":
    main()
