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

    with tempfile.TemporaryDirectory(prefix="qualia_ai_service_guard_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'service_guard.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            import services.analyzer as analyzer
            import services.mapper as mapper

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(
                    name="AI service guard smoke",
                    client="Smoke Client",
                    research_objective="Verify durable service boundaries",
                )
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Main flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section 1", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="最も重要なことは何ですか？",
                    seq=1,
                )
                db.session.add(question)
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
                mapping = UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add(mapping)
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                question_id = int(question.id)
                mapping_id = int(mapping.id)

                analyzer_provider_called = {"value": False}
                mapper_provider_called = {"value": False}
                original_analyzer_call = analyzer.call_structured
                original_mapper_call = mapper.call_structured

                def forbidden_analyzer_provider(*_args, **_kwargs):
                    analyzer_provider_called["value"] = True
                    raise AssertionError("analysis provider work must not start")

                def forbidden_mapper_provider(*_args, **_kwargs):
                    mapper_provider_called["value"] = True
                    raise AssertionError("mapping provider work must not start")

                analyzer.call_structured = forbidden_analyzer_provider
                mapper.call_structured = forbidden_mapper_provider
                try:
                    cases = [
                        ("per-question", lambda: analyzer.analyze_per_question(interview_id, question_id)),
                        ("participant-summary", lambda: analyzer.analyze_interview_summary(interview_id)),
                        ("cross-participant", lambda: analyzer.analyze_cross_participants(project_id, question_id)),
                        ("integrated", lambda: analyzer.analyze_project_integrated(project_id)),
                    ]
                    for label, invoke in cases:
                        analyzer_provider_called["value"] = False
                        before = AIAnalysis.query.count()
                        raised = False
                        try:
                            invoke()
                        except RuntimeError as exc:
                            raised = "durable result-write guard" in str(exc)
                        failures += check(
                            f"unguarded {label} save is rejected before provider work",
                            raised
                            and not analyzer_provider_called["value"]
                            and AIAnalysis.query.count() == before,
                            f"raised={raised} provider_called={analyzer_provider_called['value']} analyses={AIAnalysis.query.count()}",
                        )

                    before_mapping_count = UtteranceMapping.query.count()
                    raised = False
                    try:
                        mapper.run_mapping(interview_id)
                    except RuntimeError as exc:
                        raised = "durable result-write guard" in str(exc)
                    preserved = db.session.get(UtteranceMapping, mapping_id)
                    failures += check(
                        "unguarded mapping save is rejected before provider work",
                        raised
                        and not mapper_provider_called["value"]
                        and UtteranceMapping.query.count() == before_mapping_count
                        and preserved is not None
                        and preserved.question_id == question_id,
                        f"raised={raised} provider_called={mapper_provider_called['value']} mappings={UtteranceMapping.query.count()}",
                    )
                finally:
                    analyzer.call_structured = original_analyzer_call
                    mapper.call_structured = original_mapper_call
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
