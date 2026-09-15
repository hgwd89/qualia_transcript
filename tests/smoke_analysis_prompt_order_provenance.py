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

    with tempfile.TemporaryDirectory(prefix="qualia_analysis_prompt_order_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'prompt-order.db').as_posix()}"
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
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            import services.analyzer as analyzer

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Prompt order", client="Client", research_objective="Objective")
                db.session.add(project)
                db.session.flush()
                project_id = int(project.id)

                flow = InterviewFlow(project_id=project_id, title="Main flow", version="1.0")
                db.session.add(flow)
                db.session.flush()
                flow_id = int(flow.id)

                # Insert higher IDs first and give tied seq values. Canonical ORM
                # ordering must not depend on insertion/load order.
                section_b = InterviewFlowSection(id=20, flow_id=flow_id, title="Section B", seq=1)
                section_a = InterviewFlowSection(id=10, flow_id=flow_id, title="Section A", seq=1)
                db.session.add_all([section_b, section_a])
                db.session.flush()

                q_b = InterviewFlowQuestion(
                    id=20,
                    section_id=20,
                    question_code="QB",
                    question_text="Question B",
                    seq=1,
                )
                q_a2 = InterviewFlowQuestion(
                    id=11,
                    section_id=10,
                    question_code="QA2",
                    question_text="Question A2",
                    seq=1,
                )
                q_a1 = InterviewFlowQuestion(
                    id=10,
                    section_id=10,
                    question_code="QA1",
                    question_text="Question A1",
                    seq=1,
                )
                db.session.add_all([q_b, q_a2, q_a1])

                participant_high = Participant(
                    id=20,
                    project_id=project_id,
                    participant_code="P_HIGH",
                    display_name="High",
                )
                participant_low = Participant(
                    id=10,
                    project_id=project_id,
                    participant_code="P_LOW",
                    display_name="Low",
                )
                db.session.add_all([participant_high, participant_low])
                db.session.flush()

                interview_high = Interview(
                    id=20,
                    project_id=project_id,
                    participant_id=20,
                    flow_id=flow_id,
                    status="mapped",
                )
                interview_low = Interview(
                    id=10,
                    project_id=project_id,
                    participant_id=10,
                    flow_id=flow_id,
                    status="mapped",
                )
                db.session.add_all([interview_high, interview_low])
                db.session.flush()

                segment_id = 100
                for interview_id, prefix in ((20, "HIGH"), (10, "LOW")):
                    for question_id, suffix in ((20, "B"), (11, "A2"), (10, "A1")):
                        segment_id += 1
                        segment = Segment(
                            id=segment_id,
                            interview_id=interview_id,
                            participant_id=20 if interview_id == 20 else 10,
                            speaker_label="SPEAKER_01",
                            speaker_role="respondent",
                            text=f"{prefix}-{suffix} response with enough content",
                            seq=segment_id,
                        )
                        db.session.add(segment)
                        db.session.flush()
                        db.session.add(UtteranceMapping(
                            segment_id=segment.id,
                            question_id=question_id,
                            mapped_by="manual",
                            confidence=1.0,
                            is_unclassified=False,
                        ))
                db.session.commit()
                db.session.expire_all()

                project = db.session.get(Project, project_id)
                flow = db.session.get(InterviewFlow, flow_id)
                failures += check(
                    "project interview relationship is canonical ID order",
                    [int(row.id) for row in project.interviews] == [10, 20],
                    f"ids={[int(row.id) for row in project.interviews]}",
                )
                failures += check(
                    "flow section relationship uses seq then ID",
                    [int(row.id) for row in flow.sections] == [10, 20],
                    f"ids={[int(row.id) for row in flow.sections]}",
                )
                failures += check(
                    "section question relationship uses seq then ID",
                    [int(row.id) for row in flow.sections[0].questions] == [10, 11],
                    f"ids={[int(row.id) for row in flow.sections[0].questions]}",
                )

                captured: list[tuple[str, str]] = []
                original_call = analyzer.call_structured

                def fake_call(_system, user, _schema, schema_name="result"):
                    captured.append((schema_name, user))
                    if schema_name == "cross_analysis_result":
                        return {
                            "findings": [],
                            "common_points": "",
                            "differences": "",
                            "notable_responses": "",
                            "implications": "",
                            "unresolved": "",
                        }
                    if schema_name == "integrated_result":
                        return {
                            "findings": [],
                            "common_themes": "",
                            "key_differences": "",
                            "representative_quotes": [],
                            "implications": "",
                            "cautions": "",
                            "unresolved": "",
                        }
                    raise AssertionError(f"unexpected schema_name={schema_name}")

                analyzer.call_structured = fake_call
                try:
                    analyzer.analyze_cross_participants(
                        project_id,
                        10,
                        result_write_guard=lambda: None,
                    )
                    db.session.expire_all()
                    analyzer.analyze_project_integrated(
                        project_id,
                        result_write_guard=lambda: None,
                    )
                finally:
                    analyzer.call_structured = original_call

                cross_user = next(user for name, user in captured if name == "cross_analysis_result")
                integrated_user = next(user for name, user in captured if name == "integrated_result")

                failures += check(
                    "cross-participant provider prompt follows canonical interview ID order",
                    cross_user.find("P_LOW:") < cross_user.find("P_HIGH:"),
                    cross_user,
                )
                failures += check(
                    "integrated provider prompt follows canonical section/question order",
                    integrated_user.find("【Section A】")
                    < integrated_user.find("[QA1]")
                    < integrated_user.find("[QA2]")
                    < integrated_user.find("【Section B】"),
                    integrated_user,
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "analysis prompt order provenance smoke",
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
