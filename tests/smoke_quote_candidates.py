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
        from models.review_item import ReviewItem  # noqa: F401
        from models.segment import Segment, UtteranceMapping  # noqa: F401
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment  # noqa: F401
        from routes.interviews import bp as interviews_bp
        from routes.projects import bp as projects_bp
        from routes.settings import bp as settings_bp
        from services.quote_candidate_service import (
            QuoteCandidateNotFoundError,
            QuoteCandidateValidationError,
            create_quote_candidate,
            create_quote_candidates_from_flags,
            list_quote_candidates_for_interview,
            update_quote_candidate_status,
        )
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__, template_folder=str(repo_root / "templates"))
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "smoke-quote-candidates"
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

        project = Project(name="QuoteCandidate Smoke")
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

        text_1 = "これは引用候補として使いたい発話です。"
        seg_1 = Segment(
            interview_id=interview_1.id,
            participant_id=participant.id,
            speaker_label="C01_A",
            speaker_role="respondent",
            start_sec=0.0,
            end_sec=2.0,
            text=text_1,
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

        quote_flag = SegmentFlag(segment_id=seg_1.id, flag_type="quote", note="flag-derived")
        db.session.add(quote_flag)
        db.session.commit()

        # Service: valid create
        quote_a, created_a = create_quote_candidate(
            db.session,
            interview_id=interview_1.id,
            segment_id=seg_1.id,
            quote_text="引用候補",
            source="human",
        )
        db.session.commit()
        failures += 0 if print_result(
            "service create valid quote",
            created_a and quote_a.id is not None,
        ) else 1

        # Service: quote_text must be in segment text
        try:
            create_quote_candidate(
                db.session,
                interview_id=interview_1.id,
                segment_id=seg_1.id,
                quote_text="存在しない引用",
                source="human",
            )
            failures += 1
            print_result("service rejects quote_text not in segment", False, "unexpected success")
        except QuoteCandidateValidationError:
            print_result("service rejects quote_text not in segment", True)

        # Service: char_start/char_end match
        start = text_1.index("使いたい")
        end = start + len("使いたい")
        quote_b, created_b = create_quote_candidate(
            db.session,
            interview_id=interview_1.id,
            segment_id=seg_1.id,
            quote_text=text_1[start:end],
            source="human",
            char_start=start,
            char_end=end,
        )
        db.session.commit()
        failures += 0 if print_result(
            "service create with char range match",
            created_b and quote_b.char_start == start and quote_b.char_end == end,
        ) else 1

        # Service: char_start/char_end mismatch
        try:
            create_quote_candidate(
                db.session,
                interview_id=interview_1.id,
                segment_id=seg_1.id,
                quote_text="不一致",
                source="human",
                char_start=start,
                char_end=end,
            )
            failures += 1
            print_result("service rejects char range mismatch", False, "unexpected success")
        except QuoteCandidateValidationError:
            print_result("service rejects char range mismatch", True)

        # Service: invalid source
        try:
            create_quote_candidate(
                db.session,
                interview_id=interview_1.id,
                segment_id=seg_1.id,
                quote_text="引用候補",
                source="invalid",
            )
            failures += 1
            print_result("service rejects invalid source", False, "unexpected success")
        except QuoteCandidateValidationError:
            print_result("service rejects invalid source", True)

        # Service: segment interview mismatch
        try:
            create_quote_candidate(
                db.session,
                interview_id=interview_1.id,
                segment_id=seg_2.id,
                quote_text="別インタビュー",
                source="human",
            )
            failures += 1
            print_result("service rejects segment from other interview", False, "unexpected success")
        except QuoteCandidateNotFoundError:
            print_result("service rejects segment from other interview", True)

        # Service: from_flags + de-dup
        summary_1 = create_quote_candidates_from_flags(db.session, interview_id=interview_1.id)
        db.session.commit()
        summary_2 = create_quote_candidates_from_flags(db.session, interview_id=interview_1.id)
        db.session.commit()
        failures += 0 if print_result(
            "from_flags creates candidate",
            summary_1.get("created", 0) >= 1,
            f"summary={summary_1}",
        ) else 1
        failures += 0 if print_result(
            "from_flags does not duplicate on second run",
            summary_2.get("created", -1) == 0 and summary_2.get("existing", 0) >= 1,
            f"summary={summary_2}",
        ) else 1

        # Service: status update approved/rejected + invalid status reject
        flag_quote = (
            QuoteCandidate.query
            .filter_by(interview_id=interview_1.id, source="flag")
            .order_by(QuoteCandidate.id.asc())
            .first()
        )
        updated_approved = update_quote_candidate_status(
            db.session,
            interview_id=interview_1.id,
            quote_id=flag_quote.quote_id,
            status="approved",
        )
        db.session.commit()
        failures += 0 if print_result(
            "status update candidate->approved",
            updated_approved.status == "approved",
        ) else 1

        quote_c, _ = create_quote_candidate(
            db.session,
            interview_id=interview_1.id,
            segment_id=seg_1.id,
            quote_text="発話です。",
            source="human",
        )
        db.session.commit()
        updated_rejected = update_quote_candidate_status(
            db.session,
            interview_id=interview_1.id,
            quote_id=quote_c.quote_id,
            status="rejected",
        )
        db.session.commit()
        failures += 0 if print_result(
            "status update candidate->rejected",
            updated_rejected.status == "rejected",
        ) else 1

        try:
            update_quote_candidate_status(
                db.session,
                interview_id=interview_1.id,
                quote_id=quote_c.quote_id,
                status="open",
            )
            failures += 1
            print_result("service rejects invalid status", False, "unexpected success")
        except QuoteCandidateValidationError:
            print_result("service rejects invalid status", True)

        client = app.test_client()

        # API: list
        r_list = client.get(f"/api/interviews/{interview_1.id}/quote-candidates")
        list_json = r_list.get_json(silent=True) or {}
        failures += 0 if print_result(
            "GET quote-candidates returns 200",
            r_list.status_code == 200,
            f"status={r_list.status_code}",
        ) else 1
        failures += 0 if print_result(
            "GET quote-candidates returns items",
            bool(list_json.get("ok") and isinstance(list_json.get("items"), list)),
            f"count={list_json.get('count')}",
        ) else 1

        # API: create success
        r_create_ok = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates",
            json={
                "segment_id": seg_1.id,
                "quote_text": "これは",
                "source": "human",
            },
        )
        create_json = r_create_ok.get_json(silent=True) or {}
        failures += 0 if print_result(
            "POST quote-candidates create works",
            r_create_ok.status_code == 200 and create_json.get("ok") is True,
            f"status={r_create_ok.status_code}",
        ) else 1

        # API: create invalid quote text
        r_create_invalid_text = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates",
            json={
                "segment_id": seg_1.id,
                "quote_text": "本文にない",
                "source": "human",
            },
        )
        failures += 0 if print_result(
            "POST quote-candidates rejects unmatched quote text",
            r_create_invalid_text.status_code == 400,
            f"status={r_create_invalid_text.status_code}",
        ) else 1

        # API: create char mismatch
        r_create_char_mismatch = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates",
            json={
                "segment_id": seg_1.id,
                "quote_text": "不一致",
                "source": "human",
                "char_start": start,
                "char_end": end,
            },
        )
        failures += 0 if print_result(
            "POST quote-candidates rejects char mismatch",
            r_create_char_mismatch.status_code == 400,
            f"status={r_create_char_mismatch.status_code}",
        ) else 1

        # API: invalid source
        r_create_invalid_source = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates",
            json={
                "segment_id": seg_1.id,
                "quote_text": "引用候補",
                "source": "nope",
            },
        )
        failures += 0 if print_result(
            "POST quote-candidates rejects invalid source",
            r_create_invalid_source.status_code == 400,
            f"status={r_create_invalid_source.status_code}",
        ) else 1

        # API: segment outside interview
        r_create_other_interview = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates",
            json={
                "segment_id": seg_2.id,
                "quote_text": "別インタビュー",
                "source": "human",
            },
        )
        failures += 0 if print_result(
            "POST quote-candidates rejects other interview segment",
            r_create_other_interview.status_code == 404,
            f"status={r_create_other_interview.status_code}",
        ) else 1

        # API: from flags
        r_from_flags = client.post(f"/api/interviews/{interview_1.id}/quote-candidates/from-flags", json={})
        from_flags_json = r_from_flags.get_json(silent=True) or {}
        failures += 0 if print_result(
            "POST quote-candidates/from-flags works",
            r_from_flags.status_code == 200 and from_flags_json.get("ok") is True,
            f"status={r_from_flags.status_code}",
        ) else 1

        # API: status update
        api_target, _ = create_quote_candidate(
            db.session,
            interview_id=interview_1.id,
            segment_id=seg_1.id,
            quote_text="として",
            source="human",
        )
        db.session.commit()
        r_status_approved = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates/{api_target.quote_id}/status",
            json={"status": "approved"},
        )
        refreshed_target = db.session.get(QuoteCandidate, api_target.id)
        failures += 0 if print_result(
            "POST quote-candidate status update works",
            r_status_approved.status_code == 200 and refreshed_target.status == "approved",
            f"status_code={r_status_approved.status_code}, value={refreshed_target.status}",
        ) else 1

        # API: invalid status
        r_status_invalid = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates/{api_target.quote_id}/status",
            json={"status": "open"},
        )
        failures += 0 if print_result(
            "POST quote-candidate invalid status rejected",
            r_status_invalid.status_code == 400,
            f"status={r_status_invalid.status_code}",
        ) else 1

        quote_other, _ = create_quote_candidate(
            db.session,
            interview_id=interview_2.id,
            segment_id=seg_2.id,
            quote_text="別インタビュー",
            source="human",
        )
        db.session.commit()
        r_status_other = client.post(
            f"/api/interviews/{interview_1.id}/quote-candidates/{quote_other.quote_id}/status",
            json={"status": "approved"},
        )
        failures += 0 if print_result(
            "POST quote-candidate status rejects other interview quote",
            r_status_other.status_code == 404,
            f"status={r_status_other.status_code}",
        ) else 1

        # API: list with status filter
        r_list_approved = client.get(
            f"/api/interviews/{interview_1.id}/quote-candidates",
            query_string={"status": "approved"},
        )
        approved_json = r_list_approved.get_json(silent=True) or {}
        approved_items = approved_json.get("items") or []
        failures += 0 if print_result(
            "GET quote-candidates supports status filter",
            r_list_approved.status_code == 200 and all(i.get("status") == "approved" for i in approved_items),
            f"status={r_list_approved.status_code}, count={len(approved_items)}",
        ) else 1

        listed_all = list_quote_candidates_for_interview(db.session, interview_id=interview_1.id)
        failures += 0 if print_result(
            "service list_quote_candidates_for_interview works",
            len(listed_all) >= 1,
            f"count={len(listed_all)}",
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
