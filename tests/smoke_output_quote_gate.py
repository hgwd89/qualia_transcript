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
        from models.analysis import AIAnalysis  # noqa: F401 (mapper registry)
        from models.generated_file import GeneratedFile  # noqa: F401
        from models.interview import Interview, MediaFile, Transcription  # noqa: F401
        from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
        from models.participant import Participant, ParticipantAttribute  # noqa: F401
        from models.project import Project
        from models.quote_candidate import QuoteCandidate
        from models.review_item import ReviewItem  # noqa: F401
        from models.segment import Segment, UtteranceMapping  # noqa: F401
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment  # noqa: F401
        from services.output_quote_gate import get_approved_quote_candidates_for_interview
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()

        project = Project(name="Output Quote Gate Smoke")
        db.session.add(project)
        db.session.flush()

        participant = Participant(project_id=project.id, participant_code="P01", display_name="User")
        db.session.add(participant)
        db.session.flush()

        flow = InterviewFlow(project_id=project.id, title="Flow")
        db.session.add(flow)
        db.session.flush()

        section = InterviewFlowSection(flow_id=flow.id, title="Sec", seq=1)
        db.session.add(section)
        db.session.flush()

        question = InterviewFlowQuestion(section_id=section.id, question_code="Q1", question_text="Q1", seq=1)
        db.session.add(question)
        db.session.flush()

        interview1 = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id, status="mapped")
        interview2 = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id, status="mapped")
        db.session.add_all([interview1, interview2])
        db.session.flush()

        seg1 = Segment(
            interview_id=interview1.id,
            participant_id=participant.id,
            speaker_label="SPEAKER_00",
            speaker_role="respondent",
            start_sec=1.0,
            end_sec=3.5,
            text="approved quote text",
            seq=1,
        )
        seg2 = Segment(
            interview_id=interview1.id,
            participant_id=participant.id,
            speaker_label="SPEAKER_01",
            speaker_role="respondent",
            start_sec=3.5,
            end_sec=5.0,
            text="candidate quote text",
            seq=2,
        )
        seg3 = Segment(
            interview_id=interview2.id,
            participant_id=participant.id,
            speaker_label="SPEAKER_02",
            speaker_role="respondent",
            start_sec=0.5,
            end_sec=2.0,
            text="other interview approved quote",
            seq=1,
        )
        db.session.add_all([seg1, seg2, seg3])
        db.session.flush()

        baseline_texts = {
            seg1.id: seg1.text,
            seg2.id: seg2.text,
            seg3.id: seg3.text,
        }

        # SegmentFlag.quote alone should not become formal output quote.
        db.session.add(SegmentFlag(segment_id=seg2.id, flag_type="quote"))

        approved_q1 = QuoteCandidate(
            quote_id=f"QT-{interview1.id}-{seg1.id}-APP",
            project_id=project.id,
            interview_id=interview1.id,
            segment_id=seg1.id,
            participant_id=participant.id,
            question_id=question.id,
            start_sec=seg1.start_sec,
            end_sec=seg1.end_sec,
            quote_text=seg1.text,
            status="approved",
            source="human",
        )
        candidate_q1 = QuoteCandidate(
            quote_id=f"QT-{interview1.id}-{seg2.id}-CAN",
            project_id=project.id,
            interview_id=interview1.id,
            segment_id=seg2.id,
            participant_id=participant.id,
            question_id=question.id,
            start_sec=seg2.start_sec,
            end_sec=seg2.end_sec,
            quote_text=seg2.text,
            status="candidate",
            source="flag",
        )
        rejected_q1 = QuoteCandidate(
            quote_id=f"QT-{interview1.id}-{seg1.id}-REJ",
            project_id=project.id,
            interview_id=interview1.id,
            segment_id=seg1.id,
            participant_id=participant.id,
            question_id=question.id,
            start_sec=seg1.start_sec,
            end_sec=seg1.end_sec,
            quote_text=seg1.text,
            status="rejected",
            source="human",
        )
        approved_q2 = QuoteCandidate(
            quote_id=f"QT-{interview2.id}-{seg3.id}-APP",
            project_id=project.id,
            interview_id=interview2.id,
            segment_id=seg3.id,
            participant_id=participant.id,
            question_id=question.id,
            start_sec=seg3.start_sec,
            end_sec=seg3.end_sec,
            quote_text=seg3.text,
            status="approved",
            source="import",
        )
        db.session.add_all([approved_q1, candidate_q1, rejected_q1, approved_q2])
        db.session.commit()

        rows = get_approved_quote_candidates_for_interview(db.session, interview1.id)
        quote_ids = [r["quote_id"] for r in rows]

        failures += 0 if print_result(
            "approved quote is returned",
            approved_q1.quote_id in quote_ids,
            f"quote_ids={quote_ids}",
        ) else 1
        failures += 0 if print_result(
            "candidate quote is excluded",
            candidate_q1.quote_id not in quote_ids,
            f"quote_ids={quote_ids}",
        ) else 1
        failures += 0 if print_result(
            "rejected quote is excluded",
            rejected_q1.quote_id not in quote_ids,
            f"quote_ids={quote_ids}",
        ) else 1
        failures += 0 if print_result(
            "other interview approved quote is excluded",
            approved_q2.quote_id not in quote_ids,
            f"quote_ids={quote_ids}",
        ) else 1
        failures += 0 if print_result(
            "SegmentFlag.quote alone is not treated as formal quote",
            all(r["segment_id"] != seg2.id for r in rows),
        ) else 1

        required_keys = {
            "quote_id",
            "segment_id",
            "participant_id",
            "question_id",
            "start_sec",
            "end_sec",
            "quote_text",
            "source",
            "status",
        }
        key_check_ok = all(required_keys.issubset(set(r.keys())) for r in rows)
        failures += 0 if print_result(
            "required fields are present",
            key_check_ok,
            f"keys={sorted(list(rows[0].keys())) if rows else []}",
        ) else 1

        timestamp_ok = any((r["start_sec"] is not None and r["end_sec"] is not None) for r in rows)
        failures += 0 if print_result(
            "timestamp fields are returned",
            timestamp_ok,
        ) else 1

        post_texts = {s.id: s.text for s in Segment.query.order_by(Segment.id.asc()).all()}
        failures += 0 if print_result(
            "Segment.text remains unchanged",
            post_texts == baseline_texts,
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())

