from __future__ import annotations

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
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_mapping_scope_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'mapping-scope.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            import services.mapper as mapper_service

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                local_project = Project(name="Mapping scope local")
                foreign_project = Project(name="Mapping scope foreign")
                db.session.add_all([local_project, foreign_project])
                db.session.flush()

                local_flow = InterviewFlow(project_id=local_project.id, title="Local flow")
                foreign_flow = InterviewFlow(project_id=foreign_project.id, title="Foreign flow")
                db.session.add_all([local_flow, foreign_flow])
                db.session.flush()

                local_section = InterviewFlowSection(
                    flow_id=local_flow.id,
                    title="Local section",
                    seq=0,
                )
                foreign_section = InterviewFlowSection(
                    flow_id=foreign_flow.id,
                    title="Foreign section",
                    seq=0,
                )
                db.session.add_all([local_section, foreign_section])
                db.session.flush()

                local_question = InterviewFlowQuestion(
                    section_id=local_section.id,
                    question_code="Q1",
                    question_text="ローカル質問",
                    seq=0,
                )
                foreign_question = InterviewFlowQuestion(
                    section_id=foreign_section.id,
                    question_code="QF1",
                    question_text="別プロジェクト質問",
                    seq=0,
                )
                db.session.add_all([local_question, foreign_question])
                db.session.flush()

                local_interview = Interview(
                    project_id=local_project.id,
                    flow_id=local_flow.id,
                    status="transcribed",
                )
                foreign_interview = Interview(
                    project_id=foreign_project.id,
                    flow_id=foreign_flow.id,
                    status="transcribed",
                )
                db.session.add_all([local_interview, foreign_interview])
                db.session.flush()

                local_a = Segment(
                    interview_id=local_interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="ローカル回答A",
                    seq=0,
                )
                local_b = Segment(
                    interview_id=local_interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="ローカル回答B",
                    seq=0,
                )
                foreign_segment = Segment(
                    interview_id=foreign_interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="別プロジェクト回答",
                    seq=0,
                )
                db.session.add_all([local_a, local_b, foreign_segment])
                db.session.flush()

                original_mapping = UtteranceMapping(
                    segment_id=local_a.id,
                    question_id=local_question.id,
                    mapped_by="human",
                    confidence=1.0,
                    is_unclassified=False,
                    notes="preserve_on_reject",
                )
                db.session.add(original_mapping)
                db.session.commit()

                original_call = mapper_service.call_structured

                def run_with(payload: list[dict]):
                    mapper_service.call_structured = lambda *_args, **_kwargs: {
                        "mappings": payload
                    }
                    try:
                        return mapper_service.run_mapping(
                            local_interview.id,
                            result_write_guard=lambda: None,
                        )
                    finally:
                        mapper_service.call_structured = original_call

                def preserved_original() -> bool:
                    rows = (
                        UtteranceMapping.query
                        .filter_by(segment_id=local_a.id)
                        .order_by(UtteranceMapping.id.asc())
                        .all()
                    )
                    return (
                        len(rows) == 1
                        and rows[0].mapped_by == "human"
                        and rows[0].notes == "preserve_on_reject"
                    )

                raised = False
                try:
                    run_with([
                        {
                            "segment_id": foreign_segment.id,
                            "question_id": local_question.id,
                            "confidence": 0.9,
                            "is_unclassified": False,
                        },
                        {
                            "segment_id": local_b.id,
                            "question_id": local_question.id,
                            "confidence": 0.9,
                            "is_unclassified": False,
                        },
                    ])
                except mapper_service.MappingResultScopeError as exc:
                    raised = "segment_id outside mapping scope" in str(exc)
                failures += check(
                    "foreign segment id is rejected before canonical replacement",
                    raised and preserved_original(),
                    f"raised={raised} preserved={preserved_original()}",
                )
                db.session.rollback()

                raised = False
                try:
                    run_with([
                        {
                            "segment_id": local_a.id,
                            "question_id": foreign_question.id,
                            "confidence": 0.9,
                            "is_unclassified": False,
                        },
                        {
                            "segment_id": local_b.id,
                            "question_id": local_question.id,
                            "confidence": 0.9,
                            "is_unclassified": False,
                        },
                    ])
                except mapper_service.MappingResultScopeError as exc:
                    raised = "question_id outside interview flow" in str(exc)
                failures += check(
                    "foreign question id is rejected before canonical replacement",
                    raised and preserved_original(),
                    f"raised={raised} preserved={preserved_original()}",
                )
                db.session.rollback()

                raised = False
                try:
                    run_with([
                        {
                            "segment_id": local_a.id,
                            "question_id": local_question.id,
                            "confidence": 0.9,
                            "is_unclassified": False,
                        },
                        {
                            "segment_id": local_a.id,
                            "question_id": None,
                            "confidence": 0.2,
                            "is_unclassified": True,
                        },
                    ])
                except mapper_service.MappingResultScopeError as exc:
                    raised = "duplicate segment_id" in str(exc)
                failures += check(
                    "duplicate provider segment rows are rejected",
                    raised and preserved_original(),
                    f"raised={raised} preserved={preserved_original()}",
                )
                db.session.rollback()

                raised = False
                try:
                    run_with([
                        {
                            "segment_id": local_a.id,
                            "question_id": local_question.id,
                            "confidence": 0.9,
                            "is_unclassified": False,
                        }
                    ])
                except mapper_service.MappingResultScopeError as exc:
                    raised = "omitted respondent segment_id" in str(exc)
                failures += check(
                    "partial provider result cannot mark interview mapped",
                    raised and preserved_original(),
                    f"raised={raised} preserved={preserved_original()}",
                )
                db.session.rollback()

                valid_payload = [
                    {
                        "segment_id": local_a.id,
                        "question_id": local_question.id,
                        "confidence": 0.95,
                        "is_unclassified": False,
                    },
                    {
                        "segment_id": local_b.id,
                        "question_id": None,
                        "confidence": 0.3,
                        "is_unclassified": True,
                    },
                ]
                mapped_count = run_with(valid_payload)
                mapped_rows = (
                    UtteranceMapping.query
                    .join(Segment, UtteranceMapping.segment_id == Segment.id)
                    .filter(Segment.interview_id == local_interview.id)
                    .order_by(UtteranceMapping.segment_id.asc())
                    .all()
                )
                failures += check(
                    "complete in-scope provider result replaces local mapping set",
                    mapped_count == 2
                    and len(mapped_rows) == 2
                    and {int(row.segment_id) for row in mapped_rows}
                    == {int(local_a.id), int(local_b.id)}
                    and all(row.mapped_by == "ai" for row in mapped_rows)
                    and local_interview.status == "mapped",
                    f"mapped_count={mapped_count} rows={[row.to_dict() for row in mapped_rows]}",
                )

                # Equal historical seq values must not make provider input order
                # nondeterministic. Both local segments use seq=0 above.
                captured = {"user": ""}

                def capture_call(_system, user, *_args, **_kwargs):
                    captured["user"] = user
                    return {"mappings": valid_payload}

                mapper_service.call_structured = capture_call
                try:
                    mapper_service.run_mapping(
                        local_interview.id,
                        result_write_guard=lambda: None,
                    )
                finally:
                    mapper_service.call_structured = original_call
                first_pos = captured["user"].find(f"segment_id:{min(local_a.id, local_b.id)}")
                second_pos = captured["user"].find(f"segment_id:{max(local_a.id, local_b.id)}")
                failures += check(
                    "duplicate segment seq uses id as deterministic prompt tie-breaker",
                    first_pos >= 0 and second_pos > first_pos,
                    f"positions=({first_pos}, {second_pos})",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "mapping result scope smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if app is not None:
                try:
                    from models import db
                    with app.app_context():
                        db.session.remove()
                        db.engine.dispose()
                except Exception:
                    pass
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
