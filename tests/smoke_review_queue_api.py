import sys
from datetime import datetime
from pathlib import Path

from flask import Flask


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    try:
        from models import db
        from models.analysis import AIAnalysis
        from models.generated_file import GeneratedFile  # noqa: F401 (mapper registry)
        from models.interview import Interview
        from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
        from models.participant import Participant
        from models.project import Project
        from models.review_item import ReviewItem
        from models.segment import Segment, UtteranceMapping
        from routes.interviews import bp as interviews_bp
        from routes.projects import bp as projects_bp
        from routes.settings import bp as settings_bp
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__, template_folder=str(repo_root / "templates"))
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "smoke-review-queue"
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(projects_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(interviews_bp)

    @app.context_processor
    def inject_globals():
        return {"SERVICE_NAME": "Qualia Transcript Smoke", "now": datetime.now()}

    with app.app_context():
        db.create_all()

        project = Project(name="ReviewQueue API Smoke")
        db.session.add(project)
        db.session.flush()

        participant = Participant(project_id=project.id, participant_code="P01", display_name="Tester")
        db.session.add(participant)
        db.session.flush()

        flow = InterviewFlow(project_id=project.id, title="Flow")
        db.session.add(flow)
        db.session.flush()

        section = InterviewFlowSection(flow_id=flow.id, title="Sec", seq=1)
        db.session.add(section)
        db.session.flush()

        question = InterviewFlowQuestion(section_id=section.id, question_code="Q1", question_text="q", seq=1)
        db.session.add(question)
        db.session.flush()

        interview_1 = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id, status="mapped")
        interview_2 = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id, status="mapped")
        db.session.add_all([interview_1, interview_2])
        db.session.flush()

        seg = Segment(
            interview_id=interview_1.id,
            participant_id=participant.id,
            speaker_label="C01_A",
            speaker_role="respondent",
            start_sec=0.0,
            end_sec=1.0,
            text="review queue smoke segment",
            seq=1,
        )
        db.session.add(seg)
        db.session.flush()

        um = UtteranceMapping(
            segment_id=seg.id,
            question_id=None,
            mapped_by="ai",
            confidence=0.40,
            confidence_level="low",
            is_unclassified=True,
        )
        db.session.add(um)

        ai = AIAnalysis(
            project_id=project.id,
            interview_id=interview_1.id,
            question_id=question.id,
            analysis_type="integrated_interview_analysis",
            title="draft",
            summary_text="summary",
            content_json="{}",
            model_used="none",
            status="draft",
        )
        db.session.add(ai)

        item_other_interview = ReviewItem(
            project_id=project.id,
            interview_id=interview_2.id,
            item_type="ai_analysis_draft",
            target_type="ai_analysis",
            target_id=9999,
            severity="medium",
            status="open",
            reason="other interview item",
        )
        db.session.add(item_other_interview)
        db.session.commit()

        client = app.test_client()

        pre_count = ReviewItem.query.filter_by(interview_id=interview_1.id).count()
        r_get = client.get(f"/interviews/{interview_1.id}/review")
        post_count = ReviewItem.query.filter_by(interview_id=interview_1.id).count()
        failures += 0 if print_result(
            "GET /review returns 200",
            r_get.status_code == 200,
            f"status={r_get.status_code}",
        ) else 1
        failures += 0 if print_result(
            "GET /review does not rebuild automatically",
            pre_count == post_count == 0,
            f"before={pre_count}, after={post_count}",
        ) else 1

        r_rebuild = client.post(f"/api/interviews/{interview_1.id}/review/rebuild", json={})
        rebuild_json = r_rebuild.get_json(silent=True) or {}
        failures += 0 if print_result(
            "POST /review/rebuild returns 200",
            r_rebuild.status_code == 200,
            f"status={r_rebuild.status_code}",
        ) else 1
        failures += 0 if print_result(
            "POST /review/rebuild returns summary",
            bool(rebuild_json.get("ok") and isinstance(rebuild_json.get("summary"), dict)),
        ) else 1

        open_items = ReviewItem.query.filter_by(interview_id=interview_1.id, status="open").order_by(ReviewItem.id.asc()).all()
        failures += 0 if print_result(
            "rebuild created open review items",
            len(open_items) >= 1,
            f"open_count={len(open_items)}",
        ) else 1

        target_resolve = open_items[0] if open_items else None
        target_ignore = open_items[1] if len(open_items) > 1 else None
        if target_ignore is None and target_resolve is not None:
            target_ignore = ReviewItem(
                project_id=project.id,
                interview_id=interview_1.id,
                item_type="manual_test",
                target_type="segment",
                target_id=seg.id,
                severity="low",
                status="open",
                reason="manual item for ignore test",
            )
            db.session.add(target_ignore)
            db.session.commit()

        if target_resolve is not None:
            r_resolve = client.post(
                f"/api/interviews/{interview_1.id}/review-items/{target_resolve.id}/status",
                json={"status": "resolved"},
            )
            db.session.refresh(target_resolve)
            failures += 0 if print_result(
                "status update to resolved works",
                r_resolve.status_code == 200 and target_resolve.status == "resolved",
                f"status_code={r_resolve.status_code}, value={target_resolve.status}",
            ) else 1
        else:
            failures += 1
            print_result("status update to resolved works", False, "no target item")

        if target_ignore is not None:
            r_ignore = client.post(
                f"/api/interviews/{interview_1.id}/review-items/{target_ignore.id}/status",
                json={"status": "ignored"},
            )
            db.session.refresh(target_ignore)
            failures += 0 if print_result(
                "status update to ignored works",
                r_ignore.status_code == 200 and target_ignore.status == "ignored",
                f"status_code={r_ignore.status_code}, value={target_ignore.status}",
            ) else 1
        else:
            failures += 1
            print_result("status update to ignored works", False, "no target item")

        r_invalid = client.post(
            f"/api/interviews/{interview_1.id}/review-items/{target_resolve.id}/status",
            json={"status": "open"},
        ) if target_resolve is not None else None
        failures += 0 if print_result(
            "invalid status is rejected",
            bool(r_invalid is not None and r_invalid.status_code == 400),
            f"status={r_invalid.status_code if r_invalid is not None else 'n/a'}",
        ) else 1

        r_cross = client.post(
            f"/api/interviews/{interview_1.id}/review-items/{item_other_interview.id}/status",
            json={"status": "resolved"},
        )
        failures += 0 if print_result(
            "other interview review item update is rejected",
            r_cross.status_code in (403, 404),
            f"status={r_cross.status_code}",
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
