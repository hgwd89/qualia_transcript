from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_exact(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"patch target not found in {path}: {old[:180]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_job_admission() -> None:
    path = ROOT / "services" / "job_admission.py"
    replace_exact(
        path,
        '    "analyze": (True, False),\n    "analyze_question": (True, True),\n',
        '    "analyze": (True, False),\n    "analyze_semantic": (True, False),\n    "analyze_question": (True, True),\n',
    )


def patch_processing_jobs() -> None:
    path = ROOT / "services" / "processing_jobs.py"
    replace_exact(
        path,
        'JOB_TYPES = {"transcribe", "map", "analyze", "analyze_question", "analyze_cross", "analyze_integrated", "project_pipeline"}\n',
        'JOB_TYPES = {"transcribe", "map", "analyze", "analyze_semantic", "analyze_question", "analyze_cross", "analyze_integrated", "project_pipeline"}\n',
    )
    marker = '\n\ndef _perform_project_pipeline(job: ProcessingJob) -> dict:\n'
    semantic_handler = '''\n\ndef _perform_semantic_analysis(\n    job: ProcessingJob,\n    *,\n    max_segments: int | None = None,\n    no_ai: bool = False,\n) -> dict:\n    from models.interview import Interview\n    from services.processing_result_guard import find_completed_analysis_for_scope\n    from services.semantic_analysis import run_semantic_cluster_analysis\n\n    interview = db.session.get(Interview, job.interview_id)\n    if not interview or interview.project_id != job.project_id:\n        raise ValueError("interview not found in job project")\n\n    existing = find_completed_analysis_for_scope(job, "semantic_clusters")\n    if existing:\n        update_progress(job, "analyzing_semantic", analysis_id=existing.id, already_done=True)\n        return {"analysis_id": existing.id, "already_done": True}\n\n    update_progress(job, "analyzing_semantic")\n    result = run_semantic_cluster_analysis(\n        interview.id,\n        save=True,\n        max_segments=max_segments,\n        no_ai=no_ai,\n        result_write_guard=lambda: begin_job_result_write(job),\n    )\n    analysis_id = result.get("saved_analysis_id")\n    if not analysis_id:\n        raise RuntimeError("semantic analysis completed without a saved AIAnalysis row")\n    return {\n        "analysis_id": int(analysis_id),\n        "cluster_count": int(result.get("cluster_count") or 0),\n        "embedding_api_call_count": int(result.get("embedding_api_call_count") or 0),\n        "summary_api_call_count": int(result.get("summary_api_call_count") or 0),\n    }\n'''
    replace_exact(path, marker, semantic_handler + marker)
    replace_exact(
        path,
        '        "analyze": _perform_analysis,\n        "analyze_question": _perform_question_analysis,\n',
        '        "analyze": _perform_analysis,\n        "analyze_semantic": _perform_semantic_analysis,\n        "analyze_question": _perform_question_analysis,\n',
    )


def patch_result_guard() -> None:
    path = ROOT / "services" / "processing_result_guard.py"
    replace_exact(
        path,
        '''    if job.job_type == "analyze":\n        analysis = find_completed_analysis_for_job(job)\n        if analysis is not None:\n            result = {"analysis_id": int(analysis.id)}\n    elif job.job_type == "analyze_question":\n''',
        '''    if job.job_type == "analyze":\n        analysis = find_completed_analysis_for_job(job)\n        if analysis is not None:\n            result = {"analysis_id": int(analysis.id)}\n    elif job.job_type == "analyze_semantic":\n        analysis = find_completed_analysis_for_scope(job, "semantic_clusters")\n        if analysis is not None:\n            result = {"analysis_id": int(analysis.id)}\n    elif job.job_type == "analyze_question":\n''',
    )


def patch_semantic_service() -> None:
    path = ROOT / "services" / "semantic_analysis.py"
    replace_exact(
        path,
        '''def run_semantic_cluster_analysis(\n    interview_id: int,\n    save: bool = False,\n    max_segments: int | None = None,\n    no_ai: bool = False,\n) -> dict[str, Any]:\n    interview = db.session.get(Interview, interview_id)\n    if not interview:\n        raise ValueError(f"interview_id={interview_id} not found")\n\n''',
        '''def run_semantic_cluster_analysis(\n    interview_id: int,\n    save: bool = False,\n    max_segments: int | None = None,\n    no_ai: bool = False,\n    result_write_guard=None,\n) -> dict[str, Any]:\n    interview = db.session.get(Interview, interview_id)\n    if not interview:\n        raise ValueError(f"interview_id={interview_id} not found")\n    if save and result_write_guard is None:\n        raise RuntimeError("semantic analysis save requires a durable result-write guard")\n\n''',
    )
    replace_exact(
        path,
        '''    saved_analysis_id = None\n    if save:\n        analysis = AIAnalysis(\n''',
        '''    saved_analysis_id = None\n    if save:\n        result_write_guard()\n        analysis = AIAnalysis(\n''',
    )


def patch_semantic_cli() -> None:
    path = ROOT / "scripts" / "run_semantic_analysis.py"
    replace_exact(
        path,
        'from app import create_app\nfrom services.runtime_lock import RuntimeLockError, runtime_lock\nfrom services.semantic_analysis import run_semantic_cluster_analysis\n',
        'from app import create_app\nfrom models import db\nfrom models.interview import Interview\nfrom models.processing_job import ProcessingJob\nfrom services.job_admission import admit_processing_job\nfrom services.processing_jobs import _perform_semantic_analysis, execute_job\nfrom services.runtime_lock import RuntimeLockError, runtime_lock\nfrom services.semantic_analysis import run_semantic_cluster_analysis\n',
    )
    start = path.read_text(encoding="utf-8").index("def main() -> int:\n")
    end = path.read_text(encoding="utf-8").index("\n\nif __name__ == \"__main__\":", start)
    text = path.read_text(encoding="utf-8")
    main_block = '''def main() -> int:\n    args = parse_args()\n    save = bool(args.save)\n    if not args.save and not args.dry_run:\n        save = False  # default dry-run\n\n    try:\n        with runtime_lock("worker"):\n            app = create_app()\n            with app.app_context():\n                if not save:\n                    result = run_semantic_cluster_analysis(\n                        interview_id=args.interview_id,\n                        save=False,\n                        max_segments=args.max_segments,\n                        no_ai=bool(args.no_ai),\n                    )\n                else:\n                    interview = db.session.get(Interview, int(args.interview_id))\n                    if interview is None:\n                        raise ValueError(f"interview_id={args.interview_id} not found")\n\n                    admission = admit_processing_job(\n                        int(interview.project_id),\n                        "analyze_semantic",\n                        int(interview.id),\n                    )\n                    if admission.error:\n                        result = {\n                            "ok": False,\n                            "error_type": "JobAdmissionError",\n                            "error_message": admission.error,\n                        }\n                    elif admission.conflict_job_id is not None:\n                        result = {\n                            "ok": False,\n                            "error_type": "JobConflict",\n                            "error_message": "another processing job is active for this analysis scope",\n                            "conflict_job_id": int(admission.conflict_job_id),\n                        }\n                    elif not admission.created:\n                        job = db.session.get(ProcessingJob, int(admission.job_id))\n                        result = {\n                            "ok": True,\n                            "deduplicated": True,\n                            "job": job.to_dict() if job else None,\n                        }\n                    else:\n                        completed = execute_job(\n                            int(admission.job_id),\n                            handlers={\n                                "analyze_semantic": lambda job: _perform_semantic_analysis(\n                                    job,\n                                    max_segments=args.max_segments,\n                                    no_ai=bool(args.no_ai),\n                                )\n                            },\n                            worker_pid=os.getpid(),\n                        )\n                        result = {\n                            "ok": completed.status == "succeeded",\n                            "job": completed.to_dict(),\n                        }\n    except RuntimeLockError as exc:\n        print(json.dumps({\n            "ok": False,\n            "error_type": type(exc).__name__,\n            "error_message": str(exc),\n        }, ensure_ascii=False))\n        return 3\n    except Exception as e:\n        print(json.dumps({\n            "ok": False,\n            "error_type": type(e).__name__,\n            "error_message": str(e),\n        }, ensure_ascii=False))\n        return 1\n\n    print(json.dumps(result, ensure_ascii=False, indent=2))\n    if not result.get("ok", False):\n        return 1\n    return 0\n'''
    path.write_text(text[:start] + main_block + text[end:], encoding="utf-8")


def main() -> None:
    patch_job_admission()
    patch_processing_jobs()
    patch_result_guard()
    patch_semantic_service()
    patch_semantic_cli()
    print("semantic durable job patch applied")


if __name__ == "__main__":
    main()
