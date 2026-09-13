import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def _integrated_stub_result() -> dict:
    return {
        "findings": [
            {
                "point": "mapped evidence finding",
                "evidence_quote": "対象発言です。",
                "participant_codes": ["P01"],
                "question_codes": ["Q1"],
                "confidence": "high",
            }
        ],
        "common_themes": "theme",
        "key_differences": "difference",
        "representative_quotes": ["対象発言です。"],
        "implications": "implication",
        "cautions": "caution",
        "unresolved": "",
    }


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
        "BACKUP_DIR": config.BACKUP_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_project_flow_scope_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'flow-scope.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        config.BACKUP_DIR = str(root / "backups")

        try:
            from app import create_app
            from models import db
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
            import services.analyzer as analyzer
            from services.analysis_review import resolve_finding_source_segment_ids
            from services.project_flow_scope import (
                ProjectFlowScopeError,
                resolve_integrated_analysis_scope,
                resolve_pipeline_interview_flow,
            )
            from services.project_pipeline import ProjectPipelinePartialFailure, run_project_pipeline

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                # Multi-flow project: an unassigned interview must not be silently
                # assigned to whichever relationship row happens to be first.
                multi = Project(name="Multi Flow")
                db.session.add(multi)
                db.session.flush()
                mf1 = InterviewFlow(project_id=multi.id, title="Guide A", version="1.0")
                mf2 = InterviewFlow(project_id=multi.id, title="Guide B", version="2.0")
                db.session.add_all([mf1, mf2])
                db.session.flush()
                ms1 = InterviewFlowSection(flow_id=mf1.id, title="Section A", seq=1)
                ms2 = InterviewFlowSection(flow_id=mf2.id, title="Section B", seq=1)
                db.session.add_all([ms1, ms2])
                db.session.flush()
                mq1 = InterviewFlowQuestion(
                    section_id=ms1.id,
                    question_code="Q1",
                    question_text="Flow A question",
                    seq=1,
                )
                mq2 = InterviewFlowQuestion(
                    section_id=ms2.id,
                    question_code="Q1",
                    question_text="Flow B question",
                    seq=1,
                )
                db.session.add_all([mq1, mq2])
                participant = Participant(project_id=multi.id, participant_code="P01")
                db.session.add(participant)
                db.session.flush()
                unassigned = Interview(
                    project_id=multi.id,
                    participant_id=participant.id,
                    flow_id=None,
                    status="pending",
                )
                db.session.add(unassigned)
                db.session.commit()

                try:
                    resolve_pipeline_interview_flow(multi, unassigned)
                    helper_error = None
                except ProjectFlowScopeError as exc:
                    helper_error = exc
                failures += check(
                    "multi-flow unassigned interview has no implicit first-flow resolution",
                    helper_error is not None
                    and helper_error.code == "ambiguous_flow_assignment"
                    and unassigned.flow_id is None,
                    f"error={getattr(helper_error, 'code', None)}, flow_id={unassigned.flow_id}",
                )

                fake_job = SimpleNamespace(project_id=multi.id)
                try:
                    run_project_pipeline(fake_job, lambda *args, **kwargs: None)
                    pipeline_error = None
                except ProjectPipelinePartialFailure as exc:
                    pipeline_error = exc
                db.session.expire_all()
                unassigned_after = db.session.get(Interview, unassigned.id)
                pipeline_steps = (
                    pipeline_error.job_result["interviews"][0]["steps"]
                    if pipeline_error is not None
                    else []
                )
                failures += check(
                    "project pipeline preserves unassigned flow when multiple flows exist",
                    pipeline_error is not None
                    and unassigned_after.flow_id is None
                    and any(step.get("code") == "ambiguous_flow_assignment" for step in pipeline_steps),
                    f"flow_id={unassigned_after.flow_id}, steps={pipeline_steps}",
                )

                multi_id = int(multi.id)

            # Invalid integrated scope must be rejected before durable job creation.
            response = client.post(f"/api/projects/{multi_id}/analyze/integrated", json={})
            payload = response.get_json(silent=True) or {}
            with app.app_context():
                multi_jobs = ProcessingJob.query.filter_by(
                    project_id=multi_id,
                    job_type="analyze_integrated",
                ).count()
            failures += check(
                "multi-flow integrated analysis fails before queue/provider work",
                response.status_code == 409
                and payload.get("scope_error_code") == "integrated_multiple_configured_flows"
                and multi_jobs == 0,
                f"status={response.status_code}, payload={payload}, jobs={multi_jobs}",
            )

            page = client.get(f"/projects/{multi_id}/analysis")
            failures += check(
                "analysis page exposes questions from every configured flow",
                page.status_code == 200
                and b"Flow A question" in page.data
                and b"Flow B question" in page.data,
                f"status={page.status_code}",
            )

            with app.app_context():
                # Single-flow project: every participant interview is mapped and
                # the analyzer may proceed with an explicit canonical source set.
                single = Project(name="Single Flow")
                db.session.add(single)
                db.session.flush()
                sf = InterviewFlow(project_id=single.id, title="Only Guide", version="1.0")
                db.session.add(sf)
                db.session.flush()
                ss = InterviewFlowSection(flow_id=sf.id, title="Section", seq=1)
                db.session.add(ss)
                db.session.flush()
                sq = InterviewFlowQuestion(
                    section_id=ss.id,
                    question_code="Q1",
                    question_text="Single flow question",
                    seq=1,
                )
                db.session.add(sq)
                db.session.flush()
                sp = Participant(project_id=single.id, participant_code="P01")
                db.session.add(sp)
                db.session.flush()
                si = Interview(
                    project_id=single.id,
                    participant_id=sp.id,
                    flow_id=sf.id,
                    status="mapped",
                )
                db.session.add(si)
                db.session.flush()
                seg = Segment(
                    interview_id=si.id,
                    participant_id=sp.id,
                    speaker_label="RESP",
                    speaker_role="respondent",
                    text="対象発言です。",
                    seq=1,
                )
                db.session.add(seg)
                db.session.flush()
                db.session.add(UtteranceMapping(
                    segment_id=seg.id,
                    question_id=sq.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                ))
                db.session.commit()

                scope = resolve_integrated_analysis_scope(single)
                failures += check(
                    "single-flow mapped project resolves canonical integrated scope",
                    int(scope.flow.id) == int(sf.id)
                    and scope.interview_ids == (int(si.id),),
                    f"flow={scope.flow.id}, interviews={scope.interview_ids}",
                )

                calls = []
                original_call_structured = analyzer.call_structured
                analyzer.call_structured = lambda *args, **kwargs: (
                    calls.append((args, kwargs)) or _integrated_stub_result()
                )
                try:
                    analysis = analyzer.analyze_project_integrated(int(single.id))
                finally:
                    analyzer.call_structured = original_call_structured

                content = json.loads(analysis.content_json or "{}")
                failures += check(
                    "integrated analyzer persists exact flow and interview provenance",
                    len(calls) == 1
                    and int(content.get("source_flow_id")) == int(sf.id)
                    and content.get("source_interview_ids") == [int(si.id)]
                    and content.get("source_question_ids") == [int(sq.id)],
                    f"content={content}",
                )

                # Add a later mapped interview with the exact same participant,
                # question and quote. Approval of the older analysis must remain
                # pinned to the interview set that actually fed its provider prompt.
                late_interview = Interview(
                    project_id=single.id,
                    participant_id=sp.id,
                    flow_id=sf.id,
                    status="mapped",
                )
                db.session.add(late_interview)
                db.session.flush()
                late_segment = Segment(
                    interview_id=late_interview.id,
                    participant_id=sp.id,
                    speaker_label="RESP_LATE",
                    speaker_role="respondent",
                    text="対象発言です。",
                    seq=1,
                )
                db.session.add(late_segment)
                db.session.flush()
                db.session.add(UtteranceMapping(
                    segment_id=late_segment.id,
                    question_id=sq.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                ))
                db.session.commit()

                resolved_ids = resolve_finding_source_segment_ids(
                    analysis,
                    content["findings"][0],
                )
                failures += check(
                    "integrated approval stays pinned to generation-time interviews",
                    resolved_ids == [int(seg.id)]
                    and int(late_segment.id) not in resolved_ids,
                    f"resolved={resolved_ids}, original={seg.id}, late={late_segment.id}",
                )

                # A participant interview that is not mapped must block the paid
                # provider boundary rather than being silently omitted.
                incomplete_interview = Interview(
                    project_id=single.id,
                    participant_id=sp.id,
                    flow_id=sf.id,
                    status="transcribed",
                )
                db.session.add(incomplete_interview)
                db.session.commit()
                provider_calls = []
                original_call_structured = analyzer.call_structured
                analyzer.call_structured = lambda *args, **kwargs: (
                    provider_calls.append((args, kwargs)) or _integrated_stub_result()
                )
                try:
                    try:
                        analyzer.analyze_project_integrated(int(single.id))
                        incomplete_error = None
                    except ProjectFlowScopeError as exc:
                        incomplete_error = exc
                finally:
                    analyzer.call_structured = original_call_structured
                failures += check(
                    "unmapped participant interview blocks integrated provider work",
                    incomplete_error is not None
                    and incomplete_error.code == "integrated_interview_not_mapped"
                    and provider_calls == [],
                    f"error={getattr(incomplete_error, 'code', None)}, calls={len(provider_calls)}",
                )

        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]
            config.BACKUP_DIR = original_config["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
