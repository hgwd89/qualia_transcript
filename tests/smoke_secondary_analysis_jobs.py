import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_secondary_analysis_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'secondary.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from sqlalchemy import inspect as sa_inspect

            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.participant import Participant
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.job_admission import admit_processing_job, admit_retry_job
            from services.processing_jobs import JobLeaseLost, execute_job
            import routes.analyze as analyze_route
            import routes.analysis_view as analysis_view_route
            import services.analyzer as analyzer_service

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(
                    name="Secondary analysis smoke",
                    client="Smoke Client",
                    research_objective="Validate durable secondary analysis",
                )
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Main flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section 1", seq=1)
                db.session.add(section)
                db.session.flush()
                q1 = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="最も重要なことは何ですか？",
                    seq=1,
                )
                q2 = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q2",
                    question_text="次に重要なことは何ですか？",
                    seq=2,
                )
                db.session.add_all([q1, q2])
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant 1",
                )
                db.session.add(participant)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="一番大事なのは安心して使えることです。",
                    seq=0,
                )
                db.session.add(segment)
                db.session.flush()
                db.session.add(UtteranceMapping(
                    segment_id=segment.id,
                    question_id=q1.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                ))
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                q1_id = int(q1.id)
                q2_id = int(q2.id)

                job_cols = {c["name"] for c in sa_inspect(db.engine).get_columns("processing_jobs")}
                failures += check(
                    "processing_jobs persists question scope",
                    "question_id" in job_cols,
                    f"columns={sorted(job_cols)}",
                )

                # Service-level write guards: external AI output is complete before
                # canonical AIAnalysis rows are allowed to change.
                original_call_structured = analyzer_service.call_structured

                def fake_call_structured(_system, _user, _schema, schema_name=None):
                    finding = {
                        "point": "安心感が重要",
                        "evidence_quote": "一番大事なのは安心して使えることです。",
                        "participant_codes": ["P01"],
                        "question_codes": ["Q1"],
                        "confidence": "high",
                    }
                    if schema_name == "cross_analysis_result":
                        return {
                            "findings": [finding],
                            "common_points": "安心感",
                            "differences": "なし",
                            "notable_responses": "P01の発言",
                            "implications": "安心を訴求する",
                            "unresolved": "追加確認",
                        }
                    if schema_name == "integrated_result":
                        return {
                            "findings": [finding],
                            "common_themes": "安心感",
                            "key_differences": "なし",
                            "representative_quotes": ["P01: 安心して使える"],
                            "implications": "安心を訴求する",
                            "cautions": "N=1",
                            "unresolved": "追加確認",
                        }
                    return {
                        "findings": [finding],
                        "implications": "安心を訴求する",
                        "unresolved": "追加確認",
                    }

                analyzer_service.call_structured = fake_call_structured
                try:
                    guard_counts = []
                    before = AIAnalysis.query.count()

                    def question_guard():
                        guard_counts.append(("question", AIAnalysis.query.count()))

                    question_analysis = analyzer_service.analyze_per_question(
                        interview_id,
                        q1_id,
                        result_write_guard=question_guard,
                    )
                    failures += check(
                        "per-question analysis fences canonical write",
                        guard_counts[-1] == ("question", before)
                        and question_analysis.analysis_type == "per_question"
                        and AIAnalysis.query.count() == before + 1,
                        f"guard={guard_counts[-1]} before={before}",
                    )

                    before = AIAnalysis.query.count()

                    def cross_guard():
                        guard_counts.append(("cross", AIAnalysis.query.count()))

                    cross_analysis = analyzer_service.analyze_cross_participants(
                        project_id,
                        q1_id,
                        result_write_guard=cross_guard,
                    )
                    failures += check(
                        "cross-participant analysis fences canonical write",
                        guard_counts[-1] == ("cross", before)
                        and cross_analysis.analysis_type == "cross_participant"
                        and AIAnalysis.query.count() == before + 1,
                        f"guard={guard_counts[-1]} before={before}",
                    )

                    before = AIAnalysis.query.count()

                    def integrated_guard():
                        guard_counts.append(("integrated", AIAnalysis.query.count()))

                    integrated_analysis = analyzer_service.analyze_project_integrated(
                        project_id,
                        result_write_guard=integrated_guard,
                    )
                    failures += check(
                        "integrated analysis fences canonical write",
                        guard_counts[-1] == ("integrated", before)
                        and integrated_analysis.analysis_type == "integrated"
                        and AIAnalysis.query.count() == before + 1,
                        f"guard={guard_counts[-1]} before={before}",
                    )

                    before = AIAnalysis.query.count()
                    stale_raised = False
                    try:
                        analyzer_service.analyze_per_question(
                            interview_id,
                            q1_id,
                            result_write_guard=lambda: (_ for _ in ()).throw(
                                JobLeaseLost("simulated stale analysis writer")
                            ),
                        )
                    except JobLeaseLost:
                        stale_raised = True
                    failures += check(
                        "stale per-question writer cannot commit AIAnalysis",
                        stale_raised and AIAnalysis.query.count() == before,
                    )
                finally:
                    analyzer_service.call_structured = original_call_structured

                # Admission: same exact scope dedupes; same-interview work conflicts;
                # cross/integrated jobs are project-exclusive because their inputs span
                # all mapped interviews in the project.
                first = admit_processing_job(
                    project_id,
                    "analyze_question",
                    interview_id,
                    question_id=q1_id,
                )
                duplicate = admit_processing_job(
                    project_id,
                    "analyze_question",
                    interview_id,
                    question_id=q1_id,
                )
                other_question = admit_processing_job(
                    project_id,
                    "analyze_question",
                    interview_id,
                    question_id=q2_id,
                )
                cross_blocked = admit_processing_job(
                    project_id,
                    "analyze_cross",
                    question_id=q1_id,
                )
                failures += check(
                    "secondary analysis admission dedupes and excludes conflicting work",
                    first.created
                    and duplicate.job_id == first.job_id
                    and not duplicate.created
                    and other_question.conflict_job_id == first.job_id
                    and cross_blocked.conflict_job_id == first.job_id,
                    f"first={first} duplicate={duplicate} other={other_question} cross={cross_blocked}",
                )

                first_job = db.session.get(ProcessingJob, first.job_id)
                first_job.status = "succeeded"
                first_job.worker_pid = None
                db.session.commit()

                cross_admission = admit_processing_job(
                    project_id,
                    "analyze_cross",
                    question_id=q1_id,
                )
                cross_duplicate = admit_processing_job(
                    project_id,
                    "analyze_cross",
                    question_id=q1_id,
                )
                integrated_blocked = admit_processing_job(project_id, "analyze_integrated")
                failures += check(
                    "project-wide analysis jobs are exclusive and same-scope reusable",
                    cross_admission.created
                    and cross_duplicate.job_id == cross_admission.job_id
                    and not cross_duplicate.created
                    and integrated_blocked.conflict_job_id == cross_admission.job_id,
                )
                cross_job = db.session.get(ProcessingJob, cross_admission.job_id)
                cross_job.status = "succeeded"
                cross_job.worker_pid = None
                db.session.commit()

                integrated_admission = admit_processing_job(project_id, "analyze_integrated")
                integrated_job = db.session.get(ProcessingJob, integrated_admission.job_id)
                integrated_job.status = "succeeded"
                integrated_job.worker_pid = None
                db.session.commit()

                # Worker handler contract. Fakes still call the supplied write guard,
                # so execute_job exercises the actual durable result reservation.
                saved_question = analyzer_service.analyze_per_question
                saved_cross = analyzer_service.analyze_cross_participants
                saved_integrated = analyzer_service.analyze_project_integrated
                handler_guard_calls = []

                def fake_question(target_interview_id, target_question_id, *, result_write_guard=None):
                    if result_write_guard is not None:
                        result_write_guard()
                        handler_guard_calls.append("question")
                    analysis = AIAnalysis(
                        project_id=project_id,
                        interview_id=target_interview_id,
                        question_id=target_question_id,
                        analysis_type="per_question",
                        title="queued question",
                        summary_text="ok",
                        content_json='{"findings":[]}',
                        model_used="fake",
                    )
                    db.session.add(analysis)
                    db.session.commit()
                    return analysis

                def fake_cross(target_project_id, target_question_id, *, result_write_guard=None):
                    if result_write_guard is not None:
                        result_write_guard()
                        handler_guard_calls.append("cross")
                    analysis = AIAnalysis(
                        project_id=target_project_id,
                        interview_id=None,
                        question_id=target_question_id,
                        analysis_type="cross_participant",
                        title="queued cross",
                        summary_text="ok",
                        content_json='{"findings":[]}',
                        model_used="fake",
                    )
                    db.session.add(analysis)
                    db.session.commit()
                    return analysis

                def fake_integrated(target_project_id, *, result_write_guard=None):
                    if result_write_guard is not None:
                        result_write_guard()
                        handler_guard_calls.append("integrated")
                    analysis = AIAnalysis(
                        project_id=target_project_id,
                        interview_id=None,
                        question_id=None,
                        analysis_type="integrated",
                        title="queued integrated",
                        summary_text="ok",
                        content_json='{"findings":[]}',
                        model_used="fake",
                    )
                    db.session.add(analysis)
                    db.session.commit()
                    return analysis

                analyzer_service.analyze_per_question = fake_question
                analyzer_service.analyze_cross_participants = fake_cross
                analyzer_service.analyze_project_integrated = fake_integrated
                try:
                    question_job = admit_processing_job(
                        project_id,
                        "analyze_question",
                        interview_id,
                        question_id=q2_id,
                    )
                    completed_question = execute_job(question_job.job_id, worker_pid=8101)
                    cross_job = admit_processing_job(
                        project_id,
                        "analyze_cross",
                        question_id=q1_id,
                    )
                    completed_cross = execute_job(cross_job.job_id, worker_pid=8102)
                    integrated_job = admit_processing_job(project_id, "analyze_integrated")
                    completed_integrated = execute_job(integrated_job.job_id, worker_pid=8103)
                    failures += check(
                        "secondary analysis workers execute through result-write fence",
                        completed_question.status == "succeeded"
                        and completed_cross.status == "succeeded"
                        and completed_integrated.status == "succeeded"
                        and handler_guard_calls == ["question", "cross", "integrated"],
                        f"guards={handler_guard_calls}",
                    )

                    # Explicit failed -> retry starts a new result generation. A
                    # result committed by the failed attempt must not be adopted by
                    # the retry. Running crash-window recovery is covered separately
                    # by smoke_job_recovery_result_generation.py.
                    crash_job = ProcessingJob(
                        project_id=project_id,
                        question_id=q2_id,
                        job_type="analyze_cross",
                        status="failed",
                        attempt_count=1,
                        created_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                        error_message="explicitly failed after prior analysis commit",
                    )
                    db.session.add(crash_job)
                    db.session.flush()
                    committed = AIAnalysis(
                        project_id=project_id,
                        interview_id=None,
                        question_id=q2_id,
                        analysis_type="cross_participant",
                        title="prior attempt cross",
                        summary_text="existing",
                        content_json='{"findings":[]}',
                        model_used="fake",
                        created_at=datetime.now(timezone.utc),
                    )
                    db.session.add(committed)
                    db.session.commit()
                    crash_job_id = int(crash_job.id)
                    committed_id = int(committed.id)

                    retry = admit_retry_job(crash_job_id)
                    retry_calls = {"count": 0}

                    def retry_cross(
                        target_project_id,
                        target_question_id,
                        *,
                        result_write_guard=None,
                    ):
                        retry_calls["count"] += 1
                        if result_write_guard is not None:
                            result_write_guard()
                        analysis = AIAnalysis(
                            project_id=target_project_id,
                            interview_id=None,
                            question_id=target_question_id,
                            analysis_type="cross_participant",
                            title="fresh retry cross",
                            summary_text="retry",
                            content_json='{"findings":[]}',
                            model_used="fake",
                        )
                        db.session.add(analysis)
                        db.session.commit()
                        return analysis

                    analyzer_service.analyze_cross_participants = retry_cross
                    retried = execute_job(retry.job_id, worker_pid=8104)
                    retry_result = retried.to_dict().get("result") or {}
                    failures += check(
                        "secondary analysis retry rejects prior-attempt committed result",
                        retried.status == "succeeded"
                        and retry_result.get("analysis_id") is not None
                        and retry_result.get("analysis_id") != committed_id
                        and retry_result.get("already_done") is not True
                        and retry_calls["count"] == 1,
                        f"result={retry_result} calls={retry_calls['count']}",
                    )
                finally:
                    analyzer_service.analyze_per_question = saved_question
                    analyzer_service.analyze_cross_participants = saved_cross
                    analyzer_service.analyze_project_integrated = saved_integrated

                # Route contract: APIs only enqueue durable work and return 202.
                saved_analyze_launch = analyze_route.launch_job_or_preserve_active
                saved_view_launch = analysis_view_route.launch_job_or_preserve_active
                analyze_route.launch_job_or_preserve_active = lambda _job: (9101, None)
                analysis_view_route.launch_job_or_preserve_active = lambda _job: (9102, None)
                try:
                    client = app.test_client()
                    before_route_analyses = AIAnalysis.query.count()

                    question_resp = client.post(
                        f"/api/interviews/{interview_id}/analyze/question/{q1_id}",
                        json={},
                    )
                    question_json = question_resp.get_json() or {}
                    question_route_job = db.session.get(ProcessingJob, question_json.get("job_id"))
                    failures += check(
                        "per-question analysis API queues durable job",
                        question_resp.status_code == 202
                        and question_json.get("queued") is True
                        and question_json.get("job_type") == "analyze_question"
                        and question_route_job is not None
                        and question_route_job.question_id == q1_id
                        and AIAnalysis.query.count() == before_route_analyses,
                        f"status={question_resp.status_code} body={question_json}",
                    )
                    question_route_job.status = "succeeded"
                    question_route_job.worker_pid = None
                    db.session.commit()

                    cross_resp = client.post(
                        f"/api/projects/{project_id}/analyze/cross/{q1_id}",
                        json={},
                    )
                    cross_json = cross_resp.get_json() or {}
                    cross_route_job = db.session.get(ProcessingJob, cross_json.get("job_id"))
                    failures += check(
                        "cross analysis API queues project-exclusive durable job",
                        cross_resp.status_code == 202
                        and cross_json.get("queued") is True
                        and cross_json.get("job_type") == "analyze_cross"
                        and cross_route_job is not None
                        and cross_route_job.question_id == q1_id,
                        f"status={cross_resp.status_code} body={cross_json}",
                    )
                    cross_route_job.status = "succeeded"
                    cross_route_job.worker_pid = None
                    db.session.commit()

                    integrated_resp = client.post(
                        f"/api/projects/{project_id}/analyze/integrated",
                        json={},
                    )
                    integrated_json = integrated_resp.get_json() or {}
                    integrated_route_job = db.session.get(ProcessingJob, integrated_json.get("job_id"))
                    failures += check(
                        "integrated analysis API queues project-exclusive durable job",
                        integrated_resp.status_code == 202
                        and integrated_json.get("queued") is True
                        and integrated_json.get("job_type") == "analyze_integrated"
                        and integrated_route_job is not None,
                        f"status={integrated_resp.status_code} body={integrated_json}",
                    )

                    status_resp = client.get(
                        f"/api/projects/{project_id}/analysis-processing-status"
                    )
                    status_json = status_resp.get_json() or {}
                    failures += check(
                        "analysis processing status exposes active project analysis job",
                        status_resp.status_code == 200
                        and (status_json.get("latest_job") or {}).get("id") == integrated_route_job.id
                        and (status_json.get("latest_job") or {}).get("question_id") is None,
                        f"body={status_json}",
                    )
                finally:
                    analyze_route.launch_job_or_preserve_active = saved_analyze_launch
                    analysis_view_route.launch_job_or_preserve_active = saved_view_launch

                template_text = (
                    repo_root / "templates" / "analysis" / "index.html"
                ).read_text(encoding="utf-8")
                failures += check(
                    "analysis UI polls and resumes durable jobs",
                    "/api/processing-jobs/${jobId}" in template_text
                    and "if(!data.queued || !data.job_id)" in template_text
                    and "resumeActiveAnalysisJob" in template_text
                    and "analysis-processing-status" in template_text,
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "secondary analysis durable job smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    if failures:
        print(f"[FAIL] secondary analysis durable job smoke: {failures} failure(s)")
        return 1
    print("[PASS] secondary analysis durable job smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
