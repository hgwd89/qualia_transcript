import json
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
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_question_identity_") as tmp:
        root = Path(tmp)
        db_path = root / "question-identity.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")
        config.BACKUP_DIR = str(root / "backups")

        try:
            from sqlalchemy.exc import IntegrityError

            import app as app_module
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
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.analysis_review import (
                prepare_analysis_for_approval,
                resolve_finding_source_segment_ids,
            )
            import services.analyzer as analyzer

            app = create_app()
            app.config["TESTING"] = True
            quote = "同じ言葉でも別フローの質問なら別の研究文脈です。"

            with app.app_context():
                project = Project(name="Question identity scope")
                clean_project = Project(name="Clean project")
                db.session.add_all([project, clean_project])
                db.session.flush()

                flow1 = InterviewFlow(project_id=project.id, title="Flow v1", version="1")
                flow2 = InterviewFlow(project_id=project.id, title="Flow v2", version="2")
                flow3 = InterviewFlow(project_id=clean_project.id, title="Other project flow")
                db.session.add_all([flow1, flow2, flow3])
                db.session.flush()

                sec1 = InterviewFlowSection(flow_id=flow1.id, title="F1 S1", seq=1)
                sec1b = InterviewFlowSection(flow_id=flow1.id, title="F1 S2", seq=2)
                sec2 = InterviewFlowSection(flow_id=flow2.id, title="F2 S1", seq=1)
                sec3 = InterviewFlowSection(flow_id=flow3.id, title="F3 S1", seq=1)
                db.session.add_all([sec1, sec1b, sec2, sec3])
                db.session.flush()

                q1 = InterviewFlowQuestion(
                    section_id=sec1.id,
                    question_code="Q1",
                    question_text="v1 question",
                    seq=1,
                )
                q2 = InterviewFlowQuestion(
                    section_id=sec2.id,
                    question_code="Q1",
                    question_text="v2 question with same code",
                    seq=1,
                )
                foreign_q = InterviewFlowQuestion(
                    section_id=sec3.id,
                    question_code="Q1",
                    question_text="foreign project question",
                    seq=1,
                )
                db.session.add_all([q1, q2, foreign_q])
                db.session.commit()
                failures += check(
                    "same question code remains valid across different flows/projects",
                    q1.question_code == q2.question_code == foreign_q.question_code == "Q1",
                )

                trigger_names = {
                    row[0]
                    for row in db.session.execute(
                        db.text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='trigger' AND name LIKE 'trg_question_code_%'"
                        )
                    ).fetchall()
                }
                failures += check(
                    "question-code insert/update guards are installed",
                    trigger_names == {"trg_question_code_insert", "trg_question_code_update"},
                    f"triggers={sorted(trigger_names)}",
                )

                duplicate_rejected = False
                db.session.add(
                    InterviewFlowQuestion(
                        section_id=sec1b.id,
                        question_code="Q1",
                        question_text="must reject",
                        seq=1,
                    )
                )
                try:
                    db.session.commit()
                except IntegrityError:
                    duplicate_rejected = True
                    db.session.rollback()
                failures += check(
                    "database rejects a new same-flow duplicate question code",
                    duplicate_rejected,
                )

                q_local = InterviewFlowQuestion(
                    section_id=sec1b.id,
                    question_code="Q2",
                    question_text="update guard fixture",
                    seq=2,
                )
                db.session.add(q_local)
                db.session.commit()
                update_rejected = False
                q_local.question_code = "Q1"
                try:
                    db.session.commit()
                except IntegrityError:
                    update_rejected = True
                    db.session.rollback()
                failures += check(
                    "database rejects update into same-flow duplicate code",
                    update_rejected
                    and db.session.get(InterviewFlowQuestion, q_local.id).question_code == "Q2",
                )

                p1 = Participant(project_id=project.id, participant_code="P01")
                p2 = Participant(project_id=project.id, participant_code="P02")
                db.session.add_all([p1, p2])
                db.session.flush()
                i1 = Interview(
                    project_id=project.id,
                    participant_id=p1.id,
                    flow_id=flow1.id,
                    status="mapped",
                )
                i2 = Interview(
                    project_id=project.id,
                    participant_id=p2.id,
                    flow_id=flow2.id,
                    status="mapped",
                )
                db.session.add_all([i1, i2])
                db.session.flush()
                s1 = Segment(
                    interview_id=i1.id,
                    participant_id=p1.id,
                    speaker_label="R1",
                    speaker_role="respondent",
                    start_sec=0,
                    end_sec=1,
                    text=quote,
                    seq=1,
                )
                s2 = Segment(
                    interview_id=i2.id,
                    participant_id=p2.id,
                    speaker_label="R2",
                    speaker_role="respondent",
                    start_sec=0,
                    end_sec=1,
                    text=quote,
                    seq=1,
                )
                db.session.add_all([s1, s2])
                db.session.flush()
                db.session.add_all([
                    UtteranceMapping(
                        segment_id=s1.id,
                        question_id=q1.id,
                        mapped_by="manual",
                        confidence=1.0,
                        is_unclassified=False,
                    ),
                    UtteranceMapping(
                        segment_id=s2.id,
                        question_id=q2.id,
                        mapped_by="manual",
                        confidence=1.0,
                        is_unclassified=False,
                    ),
                ])
                db.session.commit()

                finding = {
                    "point": "scope fixture",
                    "evidence_quote": quote,
                    "participant_codes": [],
                    "question_codes": ["Q1"],
                    "confidence": "high",
                }
                exact_analysis = AIAnalysis(
                    project_id=project.id,
                    interview_id=None,
                    question_id=q1.id,
                    analysis_type="cross_participant",
                    content_json=json.dumps({"findings": [finding]}, ensure_ascii=False),
                )
                db.session.add(exact_analysis)
                db.session.commit()
                exact_ids = resolve_finding_source_segment_ids(exact_analysis, finding)
                failures += check(
                    "question-specific evidence uses exact question_id instead of same text code",
                    exact_ids == [s1.id],
                    f"ids={exact_ids}",
                )

                integrated_content = {
                    "source_flow_id": flow1.id,
                    "source_question_ids": [q1.id, q_local.id],
                    "findings": [finding],
                }
                integrated = AIAnalysis(
                    project_id=project.id,
                    interview_id=None,
                    question_id=None,
                    analysis_type="integrated",
                    content_json=json.dumps(integrated_content, ensure_ascii=False),
                )
                db.session.add(integrated)
                db.session.commit()
                integrated_ids = resolve_finding_source_segment_ids(integrated, finding)
                failures += check(
                    "integrated evidence stays inside persisted source flow",
                    integrated_ids == [s1.id],
                    f"ids={integrated_ids}",
                )

                legacy_integrated = AIAnalysis(
                    project_id=project.id,
                    interview_id=None,
                    question_id=None,
                    analysis_type="integrated",
                    content_json=json.dumps({"findings": [finding]}, ensure_ascii=False),
                )
                db.session.add(legacy_integrated)
                db.session.commit()
                _, unresolved = prepare_analysis_for_approval(legacy_integrated)
                failures += check(
                    "legacy integrated analysis fails closed when multiple flows make scope ambiguous",
                    bool(unresolved)
                    and any("source_flow_id" in str(item.get("reason")) for item in unresolved),
                    f"unresolved={unresolved}",
                )

                client = app.test_client()
                before_count = (
                    InterviewFlowQuestion.query
                    .join(InterviewFlowSection)
                    .filter(InterviewFlowSection.flow_id == flow1.id)
                    .count()
                )
                route_response = client.post(
                    f"/projects/{project.id}/flows/{flow1.id}/sections/{sec1b.id}/questions/new",
                    data={"question_code": "Q1", "question_text": "route duplicate"},
                    follow_redirects=False,
                )
                after_count = (
                    InterviewFlowQuestion.query
                    .join(InterviewFlowSection)
                    .filter(InterviewFlowSection.flow_id == flow1.id)
                    .count()
                )
                failures += check(
                    "question creation route rejects same-flow duplicate before mutation",
                    route_response.status_code == 409 and before_count == after_count,
                    f"status={route_response.status_code} count={before_count}->{after_count}",
                )

                # Preserve a historical duplicate without rewriting it: temporarily
                # remove guards, create the legacy state, then reinstall the guards.
                db.session.execute(db.text("DROP TRIGGER IF EXISTS trg_question_code_insert"))
                db.session.execute(db.text("DROP TRIGGER IF EXISTS trg_question_code_update"))
                db.session.commit()
                historical = InterviewFlowQuestion(
                    section_id=sec1b.id,
                    question_code="Q1",
                    question_text="historical duplicate",
                    seq=3,
                )
                db.session.add(historical)
                db.session.commit()
                app_module._install_question_code_guards()
                historical_count = (
                    InterviewFlowQuestion.query
                    .join(InterviewFlowSection)
                    .filter(
                        InterviewFlowSection.flow_id == flow1.id,
                        InterviewFlowQuestion.question_code == "Q1",
                    )
                    .count()
                )
                failures += check(
                    "legacy guard installation preserves historical duplicate rows",
                    historical_count == 2,
                    f"count={historical_count}",
                )

                post_legacy_rejected = False
                db.session.add(
                    InterviewFlowQuestion(
                        section_id=sec1.id,
                        question_code="Q1",
                        question_text="new duplicate after reinstall",
                        seq=4,
                    )
                )
                try:
                    db.session.commit()
                except IntegrityError:
                    post_legacy_rejected = True
                    db.session.rollback()
                failures += check(
                    "reinstalled legacy guards reject future duplicates",
                    post_legacy_rejected and historical_count == 2,
                )

                provider_calls = {"count": 0}
                original_call_structured = analyzer.call_structured

                def fake_call_structured(system, user, schema, schema_name=None):
                    provider_calls["count"] += 1
                    if schema is analyzer.INTEGRATED_SCHEMA:
                        return {
                            "findings": [{
                                "point": "integrated fixture",
                                "evidence_quote": quote,
                                "participant_codes": [],
                                "question_codes": ["Q1", "UNKNOWN"],
                                "confidence": "high",
                            }],
                            "common_themes": "",
                            "key_differences": "",
                            "representative_quotes": [],
                            "implications": "fixture",
                            "cautions": "",
                            "unresolved": "",
                        }
                    return {
                        "findings": [{
                            "point": "cross fixture",
                            "evidence_quote": quote,
                            "participant_codes": [],
                            "question_codes": ["HALLUCINATED"],
                            "confidence": "high",
                        }],
                        "common_points": "",
                        "differences": "",
                        "notable_responses": "",
                        "implications": "fixture",
                        "unresolved": "",
                    }

                analyzer.call_structured = fake_call_structured
                try:
                    cross = analyzer.analyze_cross_participants(project.id, q1.id)
                    cross_content = json.loads(cross.content_json)
                    failures += check(
                        "cross analysis persists canonical question identity instead of model label",
                        cross.question_id == q1.id
                        and cross_content.get("question_id") == q1.id
                        and cross_content["findings"][0].get("question_codes") == ["Q1"],
                        f"content={cross_content}",
                    )

                    before_foreign_calls = provider_calls["count"]
                    foreign_rejected = False
                    try:
                        analyzer.analyze_cross_participants(project.id, foreign_q.id)
                    except ValueError:
                        foreign_rejected = True
                    failures += check(
                        "cross analysis rejects foreign-project question before provider call",
                        foreign_rejected and provider_calls["count"] == before_foreign_calls,
                        f"calls={before_foreign_calls}->{provider_calls['count']}",
                    )

                    integrated_generated = analyzer.analyze_project_integrated(project.id)
                    generated_content = json.loads(integrated_generated.content_json)
                    failures += check(
                        "integrated analysis persists source flow and filters unknown question codes",
                        generated_content.get("source_flow_id") == flow1.id
                        and q1.id in (generated_content.get("source_question_ids") or [])
                        and generated_content["findings"][0].get("question_codes") == ["Q1"],
                        f"content={generated_content}",
                    )
                finally:
                    analyzer.call_structured = original_call_structured

                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
