import sys
import tempfile
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

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_result_write_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'result_write.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.processing_jobs import (
                JobLeaseLost,
                _claim_pending_job,
                begin_job_result_write,
            )
            import services.analyzer as analyzer_service
            import services.mapper as mapper_service

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Result write fencing")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Question",
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()
                interview = Interview(project_id=project.id, flow_id=flow.id, status="transcribed")
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    text="answer",
                    seq=0,
                )
                db.session.add(segment)
                db.session.flush()
                old_mapping = UtteranceMapping(
                    segment_id=segment.id,
                    question_id=None,
                    mapped_by="manual",
                    confidence=0.2,
                    is_unclassified=True,
                )
                db.session.add(old_mapping)
                db.session.commit()
                project_id = project.id
                interview_id = interview.id
                segment_id = segment.id
                question_id = question.id
                old_mapping_id = old_mapping.id

                original_mapper_call = mapper_service.call_structured
                mapper_service.call_structured = lambda *_args, **_kwargs: {
                    "mappings": [{
                        "segment_id": segment_id,
                        "question_id": question_id,
                        "confidence": 0.95,
                        "is_unclassified": False,
                    }]
                }
                try:
                    stale_map_job = ProcessingJob(
                        project_id=project_id,
                        interview_id=interview_id,
                        job_type="map",
                        status="pending",
                    )
                    db.session.add(stale_map_job)
                    db.session.commit()
                    stale_map, claimed = _claim_pending_job(stale_map_job.id, worker_pid=7101)
                    stale_map.status = "failed"
                    stale_map.worker_pid = None
                    db.session.commit()

                    map_rejected = False
                    try:
                        mapper_service.run_mapping(
                            interview_id,
                            result_write_guard=lambda: begin_job_result_write(stale_map),
                        )
                    except JobLeaseLost:
                        map_rejected = True

                    mappings_after_stale = UtteranceMapping.query.filter_by(segment_id=segment_id).all()
                    iv_after_stale = db.session.get(Interview, interview_id)
                    failures += check(
                        "stale mapping result cannot replace canonical mapping",
                        claimed
                        and map_rejected
                        and len(mappings_after_stale) == 1
                        and mappings_after_stale[0].id == old_mapping_id
                        and mappings_after_stale[0].mapped_by == "manual"
                        and iv_after_stale.status == "transcribed",
                        f"mappings={[m.id for m in mappings_after_stale]} status={iv_after_stale.status}",
                    )

                    current_map_job = ProcessingJob(
                        project_id=project_id,
                        interview_id=interview_id,
                        job_type="map",
                        status="pending",
                    )
                    db.session.add(current_map_job)
                    db.session.commit()
                    current_map, current_claimed = _claim_pending_job(current_map_job.id, worker_pid=7102)
                    mapped_count = mapper_service.run_mapping(
                        interview_id,
                        result_write_guard=lambda: begin_job_result_write(current_map),
                    )
                    mappings_after_current = UtteranceMapping.query.filter_by(segment_id=segment_id).all()
                    iv_after_current = db.session.get(Interview, interview_id)
                    failures += check(
                        "current mapping result commits under fenced write reservation",
                        current_claimed
                        and mapped_count == 1
                        and len(mappings_after_current) == 1
                        and mappings_after_current[0].id != old_mapping_id
                        and mappings_after_current[0].question_id == question_id
                        and iv_after_current.status == "mapped",
                        f"count={mapped_count} status={iv_after_current.status}",
                    )
                finally:
                    mapper_service.call_structured = original_mapper_call

                # Reset to mapped so participant analysis is eligible.
                interview = db.session.get(Interview, interview_id)
                interview.status = "mapped"
                db.session.commit()

                original_analyzer_call = analyzer_service.call_structured
                analyzer_service.call_structured = lambda *_args, **_kwargs: {
                    "findings": [],
                    "implications": "implication",
                    "unresolved": "none",
                }
                try:
                    stale_analysis_job = ProcessingJob(
                        project_id=project_id,
                        interview_id=interview_id,
                        job_type="analyze",
                        status="pending",
                    )
                    db.session.add(stale_analysis_job)
                    db.session.commit()
                    stale_analysis, analysis_claimed = _claim_pending_job(
                        stale_analysis_job.id,
                        worker_pid=7201,
                    )
                    stale_analysis.status = "failed"
                    stale_analysis.worker_pid = None
                    db.session.commit()

                    analysis_rejected = False
                    try:
                        analyzer_service.analyze_interview_summary(
                            interview_id,
                            result_write_guard=lambda: begin_job_result_write(stale_analysis),
                        )
                    except JobLeaseLost:
                        analysis_rejected = True

                    stale_analysis_rows = AIAnalysis.query.filter_by(
                        interview_id=interview_id,
                        analysis_type="per_participant",
                    ).all()
                    iv_after_stale_analysis = db.session.get(Interview, interview_id)
                    failures += check(
                        "stale participant analysis cannot become canonical",
                        analysis_claimed
                        and analysis_rejected
                        and len(stale_analysis_rows) == 0
                        and iv_after_stale_analysis.status == "mapped",
                        f"analyses={len(stale_analysis_rows)} status={iv_after_stale_analysis.status}",
                    )

                    current_analysis_job = ProcessingJob(
                        project_id=project_id,
                        interview_id=interview_id,
                        job_type="analyze",
                        status="pending",
                    )
                    db.session.add(current_analysis_job)
                    db.session.commit()
                    current_analysis, current_analysis_claimed = _claim_pending_job(
                        current_analysis_job.id,
                        worker_pid=7202,
                    )
                    analysis = analyzer_service.analyze_interview_summary(
                        interview_id,
                        result_write_guard=lambda: begin_job_result_write(current_analysis),
                    )
                    iv_after_current_analysis = db.session.get(Interview, interview_id)
                    failures += check(
                        "current participant analysis commits under fenced write reservation",
                        current_analysis_claimed
                        and analysis.id is not None
                        and iv_after_current_analysis.status == "analyzed"
                        and AIAnalysis.query.filter_by(
                            interview_id=interview_id,
                            analysis_type="per_participant",
                        ).count() == 1,
                        f"analysis_id={analysis.id} status={iv_after_current_analysis.status}",
                    )
                finally:
                    analyzer_service.call_structured = original_analyzer_call

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check("result write fencing smoke", False, f"{type(exc).__name__}: {exc}")
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
