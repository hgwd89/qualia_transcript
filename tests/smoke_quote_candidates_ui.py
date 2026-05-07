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
        from models.analysis import AIAnalysis  # noqa: F401 (mapper registry)
        from models.generated_file import GeneratedFile  # noqa: F401 (mapper registry)
        from models.interview import Interview
        from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
        from models.participant import Participant
        from models.project import Project
        from models.quote_candidate import QuoteCandidate
        from models.review_item import ReviewItem
        from models.segment import Segment, UtteranceMapping  # noqa: F401
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment  # noqa: F401
        from routes.interviews import bp as interviews_bp
        from routes.projects import bp as projects_bp
        from routes.settings import bp as settings_bp
        from services.quote_candidate_service import create_quote_candidate
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__, template_folder=str(repo_root / "templates"))
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "smoke-quote-candidates-ui"
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

        project = Project(name="QuoteCandidate UI Smoke")
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

        seg_1 = Segment(
            interview_id=interview_1.id,
            participant_id=participant.id,
            speaker_label="C01_A",
            speaker_role="respondent",
            start_sec=0.0,
            end_sec=2.0,
            text="これは引用候補として使いたい発話です。",
            seq=1,
        )
        seg_2 = Segment(
            interview_id=interview_2.id,
            participant_id=participant.id,
            speaker_label="C02_A",
            speaker_role="respondent",
            start_sec=2.0,
            end_sec=4.0,
            text="別インタビューの発話です。",
            seq=1,
        )
        db.session.add_all([seg_1, seg_2])
        db.session.flush()

        qc_1, _ = create_quote_candidate(
            db.session,
            interview_id=interview_1.id,
            segment_id=seg_1.id,
            quote_text="引用候補",
            source="human",
        )
        qc_2, _ = create_quote_candidate(
            db.session,
            interview_id=interview_1.id,
            segment_id=seg_1.id,
            quote_text="発話です。",
            source="human",
        )
        qc_other, _ = create_quote_candidate(
            db.session,
            interview_id=interview_2.id,
            segment_id=seg_2.id,
            quote_text="別インタビュー",
            source="human",
        )

        qflag = SegmentFlag(segment_id=seg_1.id, flag_type="quote", note="for from_flags")
        db.session.add(qflag)

        review_item = ReviewItem(
            project_id=project.id,
            interview_id=interview_1.id,
            item_type="quote_candidate",
            target_type="quote_candidate",
            target_id=qc_1.id,
            severity="medium",
            status="open",
            reason="quote candidate review needed",
        )
        db.session.add(review_item)
        db.session.commit()

        client = app.test_client()

        # QuoteCandidate list UI
        r_list = client.get(f"/interviews/{interview_1.id}/quote-candidates")
        html_list = r_list.get_data(as_text=True)
        failures += 0 if print_result(
            "GET quote-candidates UI returns 200",
            r_list.status_code == 200,
            f"status={r_list.status_code}",
        ) else 1
        failures += 0 if print_result(
            "quote-candidates UI contains quote_id",
            qc_1.quote_id in html_list,
        ) else 1

        # detail link
        r_detail = client.get(f"/interviews/{interview_1.id}")
        html_detail = r_detail.get_data(as_text=True)
        failures += 0 if print_result(
            "detail page has quote candidates link",
            f"/interviews/{interview_1.id}/quote-candidates" in html_detail,
        ) else 1

        # review queue link
        r_review = client.get(f"/interviews/{interview_1.id}/review")
        html_review = r_review.get_data(as_text=True)
        failures += 0 if print_result(
            "review queue has quote candidate link",
            f"/interviews/{interview_1.id}/quote-candidates?focus_id={qc_1.id}" in html_review,
        ) else 1

        # from-flags create and dedup
        before_flag_count = QuoteCandidate.query.filter_by(interview_id=interview_1.id, source="flag").count()
        r_from_flags_1 = client.post(f"/interviews/{interview_1.id}/quote-candidates/from-flags", follow_redirects=True)
        after_flag_count_1 = QuoteCandidate.query.filter_by(interview_id=interview_1.id, source="flag").count()
        failures += 0 if print_result(
            "from-flags creates candidate in UI route",
            r_from_flags_1.status_code == 200 and after_flag_count_1 == before_flag_count + 1,
            f"before={before_flag_count}, after={after_flag_count_1}",
        ) else 1

        r_from_flags_2 = client.post(f"/interviews/{interview_1.id}/quote-candidates/from-flags", follow_redirects=True)
        after_flag_count_2 = QuoteCandidate.query.filter_by(interview_id=interview_1.id, source="flag").count()
        failures += 0 if print_result(
            "from-flags second run does not duplicate",
            r_from_flags_2.status_code == 200 and after_flag_count_2 == after_flag_count_1,
            f"first={after_flag_count_1}, second={after_flag_count_2}",
        ) else 1

        # status approved
        r_approved = client.post(
            f"/interviews/{interview_1.id}/quote-candidates/{qc_1.quote_id}/status",
            data={"status": "approved"},
            follow_redirects=True,
        )
        db.session.refresh(qc_1)
        failures += 0 if print_result(
            "UI status update approved works",
            r_approved.status_code == 200 and qc_1.status == "approved",
            f"status_code={r_approved.status_code}, value={qc_1.status}",
        ) else 1

        # status rejected
        r_rejected = client.post(
            f"/interviews/{interview_1.id}/quote-candidates/{qc_2.quote_id}/status",
            data={"status": "rejected"},
            follow_redirects=True,
        )
        db.session.refresh(qc_2)
        failures += 0 if print_result(
            "UI status update rejected works",
            r_rejected.status_code == 200 and qc_2.status == "rejected",
            f"status_code={r_rejected.status_code}, value={qc_2.status}",
        ) else 1

        # invalid status
        r_invalid = client.post(
            f"/interviews/{interview_1.id}/quote-candidates/{qc_2.quote_id}/status",
            data={"status": "open"},
            follow_redirects=False,
        )
        failures += 0 if print_result(
            "UI invalid status rejected with 400",
            r_invalid.status_code == 400,
            f"status={r_invalid.status_code}",
        ) else 1

        # cross interview reject
        r_cross = client.post(
            f"/interviews/{interview_1.id}/quote-candidates/{qc_other.quote_id}/status",
            data={"status": "approved"},
            follow_redirects=False,
        )
        failures += 0 if print_result(
            "UI status update rejects other interview quote with 404",
            r_cross.status_code == 404,
            f"status={r_cross.status_code}",
        ) else 1

        # Existing JSON API still alive
        r_json_api = client.get(f"/api/interviews/{interview_1.id}/quote-candidates")
        j = r_json_api.get_json(silent=True) or {}
        failures += 0 if print_result(
            "existing JSON quote-candidates API still works",
            r_json_api.status_code == 200 and j.get("ok") is True,
            f"status={r_json_api.status_code}",
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
