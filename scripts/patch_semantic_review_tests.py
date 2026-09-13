from pathlib import Path

from patch_semantic_review_fixes import ROOT, main as apply_production_fixes, replace_exact


def patch_test() -> None:
    path = ROOT / "tests" / "smoke_semantic_analysis_job_fencing.py"
    replace_exact(
        path,
        '            from services.job_admission import admit_processing_job\n',
        '            from services.job_admission import admit_processing_job, admit_retry_job\n',
    )
    replace_exact(
        path,
        '            from services.processing_jobs import execute_job\n',
        '            from services.processing_jobs import execute_job\n',
    )
    replace_exact(
        path,
        '            import services.semantic_analysis as semantic_analysis\n\n            app = create_app()\n',
        '            import services.semantic_analysis as semantic_analysis\n\n            readiness_source = (repo_root / "scripts" / "audit_production_readiness_v2.py").read_text(encoding="utf-8")\n\n            app = create_app()\n',
    )
    replace_exact(
        path,
        '''                admission = admit_processing_job(\n                    project.id,\n                    "analyze_semantic",\n                    interview.id,\n                )\n''',
        '''                admission = admit_processing_job(\n                    project.id,\n                    "analyze_semantic",\n                    interview.id,\n                    request_payload={"max_segments": 1, "no_ai": True},\n                )\n''',
    )
    replace_exact(
        path,
        '''                failures += check(\n                    "semantic analysis is admitted as a durable interview job",\n                    admission.created and admission.job_id is not None and admission.error is None,\n                    str(admission),\n                )\n\n                conflicting = admit_processing_job(project.id, "analyze", interview.id)\n''',
        '''                durable_job = db.session.get(ProcessingJob, int(admission.job_id))\n                failures += check(\n                    "semantic analysis is admitted with durable request parameters",\n                    admission.created\n                    and admission.job_id is not None\n                    and admission.error is None\n                    and durable_job.to_dict().get("request") == {"max_segments": 1, "no_ai": True},\n                    f"admission={admission} job={durable_job.to_dict()}",\n                )\n                failures += check(\n                    "semantic job type is accepted by readiness scope audit",\n                    '"analyze_semantic": (True, False)' in readiness_source,\n                    "rule source present=" + str('"analyze_semantic": (True, False)' in readiness_source),\n                )\n\n                duplicate_same_request = admit_processing_job(\n                    project.id,\n                    "analyze_semantic",\n                    interview.id,\n                    request_payload={"max_segments": 1, "no_ai": True},\n                )\n                failures += check(\n                    "same semantic scope and request deduplicates",\n                    duplicate_same_request.job_id == admission.job_id\n                    and not duplicate_same_request.created\n                    and duplicate_same_request.conflict_job_id is None,\n                    str(duplicate_same_request),\n                )\n\n                mismatched_request = admit_processing_job(\n                    project.id,\n                    "analyze_semantic",\n                    interview.id,\n                    request_payload={"max_segments": 2, "no_ai": True},\n                )\n                failures += check(\n                    "same semantic scope with different request is a conflict",\n                    mismatched_request.conflict_job_id == admission.job_id,\n                    str(mismatched_request),\n                )\n\n                conflicting = admit_processing_job(project.id, "analyze", interview.id)\n''',
    )
    replace_exact(
        path,
        '''                    completed = execute_job(\n                        int(admission.job_id),\n                        handlers={\n                            "analyze_semantic": lambda job: processing_jobs._perform_semantic_analysis(\n                                job,\n                                no_ai=True,\n                            )\n                        },\n                        worker_pid=4242,\n                    )\n''',
        '''                    completed = execute_job(\n                        int(admission.job_id),\n                        worker_pid=4242,\n                    )\n''',
    )
    replace_exact(
        path,
        '''                failures += check(\n                    "durable semantic handler saves through current job lease",\n                    completed.status == "succeeded" and semantic_row is not None,\n                    f"job={completed.to_dict()} analysis_id={getattr(semantic_row, 'id', None)}",\n                )\n\n                recovery_job = ProcessingJob(\n''',
        '''                failures += check(\n                    "durable semantic handler restores persisted request parameters",\n                    completed.status == "succeeded"\n                    and semantic_row is not None\n                    and semantic_row.model_used.endswith("no-ai-summary")\n                    and completed.to_dict().get("request") == {"max_segments": 1, "no_ai": True},\n                    f"job={completed.to_dict()} analysis_id={getattr(semantic_row, 'id', None)} model={getattr(semantic_row, 'model_used', None)}",\n                )\n\n                retry_source = ProcessingJob(\n                    project_id=project.id,\n                    interview_id=interview.id,\n                    job_type="analyze_semantic",\n                    status="failed",\n                    request_json='{"max_segments":3,"no_ai":true}',\n                    error_message="retry smoke",\n                )\n                db.session.add(retry_source)\n                db.session.commit()\n                retry_admission = admit_retry_job(retry_source.id)\n                retried = db.session.get(ProcessingJob, retry_source.id)\n                failures += check(\n                    "failed semantic retry preserves durable request parameters",\n                    retry_admission.created\n                    and retried.status == "pending"\n                    and retried.to_dict().get("request") == {"max_segments": 3, "no_ai": True},\n                    f"admission={retry_admission} job={retried.to_dict()}",\n                )\n                retried.status = "failed"\n                retried.finished_at = datetime.now(timezone.utc)\n                db.session.commit()\n\n                recovery_job = ProcessingJob(\n''',
    )
    replace_exact(
        path,
        '''                active = ProcessingJob(\n                    project_id=project.id,\n''',
        '''                empty_interview = Interview(\n                    project_id=project.id,\n                    participant_id=participant.id,\n                    status="mapped",\n                )\n                db.session.add(empty_interview)\n                db.session.commit()\n                empty_admission = admit_processing_job(\n                    project.id,\n                    "analyze_semantic",\n                    empty_interview.id,\n                    request_payload={"max_segments": None, "no_ai": True},\n                )\n                empty_result = execute_job(int(empty_admission.job_id), worker_pid=5252)\n                failures += check(\n                    "semantic empty-input failure preserves actionable diagnostics",\n                    empty_result.status == "failed"\n                    and "no_fragments_after_filter" in str(empty_result.error_message)\n                    and "candidate_segment_count" in str(empty_result.error_message),\n                    str(empty_result.error_message),\n                )\n\n                active = ProcessingJob(\n                    project_id=project.id,\n''',
    )


def main() -> None:
    apply_production_fixes()
    patch_test()
    print("semantic review fixes and regressions applied")


if __name__ == "__main__":
    main()
