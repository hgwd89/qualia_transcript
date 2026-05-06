import sys
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
        from models.project import Project
        from models.participant import Participant
        from models.interview import Interview
        from models.interview_flow import InterviewFlow, InterviewFlowSection, InterviewFlowQuestion
        from models.segment import Segment
        from models.segment_flag import SegmentFlag  # noqa: F401 (mapper registry)
        from models.speaker_assignment import SpeakerAssignment  # noqa: F401
        from models.analysis import AIAnalysis
        from models.generated_file import GeneratedFile  # noqa: F401
        from models.quote_candidate import QuoteCandidate
        from models.review_item import ReviewItem
        from models.api_usage_log import APIUsageLog
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()

        project = Project(name="Smoke v0.2")
        db.session.add(project)
        db.session.flush()

        participant = Participant(
            project_id=project.id,
            participant_code="P01",
            display_name="Smoke User",
        )
        db.session.add(participant)
        db.session.flush()

        flow = InterviewFlow(project_id=project.id, title="Flow")
        db.session.add(flow)
        db.session.flush()

        section = InterviewFlowSection(flow_id=flow.id, title="Sec", seq=1)
        db.session.add(section)
        db.session.flush()

        question = InterviewFlowQuestion(
            section_id=section.id,
            question_code="Q1",
            question_text="テスト質問",
            seq=1,
        )
        db.session.add(question)
        db.session.flush()

        interview = Interview(
            project_id=project.id,
            participant_id=participant.id,
            flow_id=flow.id,
            status="pending",
        )
        db.session.add(interview)
        db.session.flush()

        segment = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="SPEAKER_00",
            speaker_role="respondent",
            start_sec=0.0,
            end_sec=2.0,
            text="これは引用候補になる発話です。",
            seq=1,
        )
        db.session.add(segment)
        db.session.flush()

        ai = AIAnalysis(
            project_id=project.id,
            interview_id=interview.id,
            question_id=question.id,
            analysis_type="per_question",
            title="Smoke Analysis",
            summary_text="summary",
            content_json="{}",
            model_used="none",
        )
        db.session.add(ai)
        db.session.flush()

        quote = QuoteCandidate(
            quote_id=f"QT-{interview.id}-{segment.id}-001",
            project_id=project.id,
            interview_id=interview.id,
            segment_id=segment.id,
            participant_id=participant.id,
            question_id=question.id,
            start_sec=segment.start_sec,
            end_sec=segment.end_sec,
            quote_text=segment.text,
            status="candidate",
            source="flag",
            note="smoke",
        )
        db.session.add(quote)

        review_open = ReviewItem(
            project_id=project.id,
            interview_id=interview.id,
            item_type="quote_candidate",
            target_type="quote_candidate",
            target_id=1,
            severity="medium",
            status="open",
        )
        review_resolved = ReviewItem(
            project_id=project.id,
            interview_id=interview.id,
            item_type="ai_analysis_draft",
            target_type="ai_analysis",
            target_id=1,
            severity="low",
            status="resolved",
        )
        review_ignored = ReviewItem(
            project_id=project.id,
            interview_id=interview.id,
            item_type="speaker_unassigned",
            target_type="speaker_assignment",
            target_id=1,
            severity="high",
            status="ignored",
        )
        db.session.add_all([review_open, review_resolved, review_ignored])

        usage = APIUsageLog(
            project_id=project.id,
            interview_id=interview.id,
            analysis_id=ai.id,
            provider="openai",
            model="gpt-4o-mini",
            operation_type="analysis_dry_run",
            request_count=1,
            success=True,
        )
        db.session.add(usage)
        db.session.commit()

        quote_row = QuoteCandidate.query.first()
        failures += 0 if print_result(
            "QuoteCandidate can be created",
            quote_row is not None,
        ) else 1
        failures += 0 if print_result(
            "QuoteCandidate.quote_text is non-empty",
            bool((quote_row.quote_text if quote_row else "").strip()),
        ) else 1

        review_statuses = {
            r.status for r in ReviewItem.query.order_by(ReviewItem.id.asc()).all()
        }
        failures += 0 if print_result(
            "ReviewItem supports open/resolved/ignored",
            {"open", "resolved", "ignored"}.issubset(review_statuses),
            f"statuses={sorted(review_statuses)}",
        ) else 1

        usage_row = APIUsageLog.query.first()
        failures += 0 if print_result(
            "APIUsageLog can be created",
            usage_row is not None and usage_row.request_count == 1,
        ) else 1

        ai_row = AIAnalysis.query.get(ai.id)
        failures += 0 if print_result(
            "AIAnalysis.status default is draft",
            (ai_row.status == "draft") if ai_row else False,
            f"status={ai_row.status if ai_row else None}",
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
