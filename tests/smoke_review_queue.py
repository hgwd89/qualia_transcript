import subprocess
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


def count_open_by_type(rows):
    counts = {}
    for row in rows:
        counts[row.item_type] = counts.get(row.item_type, 0) + 1
    return counts


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
        from models.quote_candidate import QuoteCandidate
        from models.review_item import ReviewItem
        from models.segment import Segment, UtteranceMapping
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment
        from services.review_queue import rebuild_review_items_for_interview
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()

        project = Project(name="RQ Smoke")
        db.session.add(project)
        db.session.flush()

        participant = Participant(project_id=project.id, participant_code="P01", display_name="RQ User")
        db.session.add(participant)
        db.session.flush()

        flow = InterviewFlow(project_id=project.id, title="Flow")
        db.session.add(flow)
        db.session.flush()

        section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
        db.session.add(section)
        db.session.flush()

        question = InterviewFlowQuestion(section_id=section.id, question_code="Q1", question_text="Q", seq=1)
        db.session.add(question)
        db.session.flush()

        interview = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id, status="mapped")
        db.session.add(interview)
        db.session.flush()

        seg_unclassified = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="C01_A",
            speaker_role="respondent",
            start_sec=0.0,
            end_sec=2.0,
            text="unclassified row",
            seq=1,
        )
        seg_medium = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="C01_B",
            speaker_role="respondent",
            start_sec=2.0,
            end_sec=4.0,
            text="medium confidence row",
            seq=2,
        )
        seg_low = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="C01_C",
            speaker_role="respondent",
            start_sec=4.0,
            end_sec=6.0,
            text="low confidence row",
            seq=3,
        )
        seg_needs = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="C01_D",
            speaker_role="respondent",
            start_sec=6.0,
            end_sec=8.0,
            text="needs review row",
            seq=4,
        )
        db.session.add_all([seg_unclassified, seg_medium, seg_low, seg_needs])
        db.session.flush()

        map_unclassified = UtteranceMapping(
            segment_id=seg_unclassified.id,
            question_id=None,
            mapped_by="ai",
            confidence=0.30,
            confidence_level="low",
            is_unclassified=True,
        )
        map_medium = UtteranceMapping(
            segment_id=seg_medium.id,
            question_id=question.id,
            mapped_by="ai",
            confidence=0.65,
            confidence_level="medium",
            is_unclassified=False,
        )
        map_low = UtteranceMapping(
            segment_id=seg_low.id,
            question_id=question.id,
            mapped_by="ai",
            confidence=0.30,
            confidence_level="low",
            is_unclassified=False,
        )
        db.session.add_all([map_unclassified, map_medium, map_low])
        db.session.flush()

        speaker = SpeakerAssignment(
            interview_id=interview.id,
            speaker_label="C01_C",
            speaker_role="respondent",
            participant_id=None,
        )
        db.session.add(speaker)
        db.session.flush()

        quote_candidate = QuoteCandidate(
            quote_id=f"QT-{interview.id}-{seg_medium.id}-001",
            project_id=project.id,
            interview_id=interview.id,
            segment_id=seg_medium.id,
            participant_id=participant.id,
            question_id=question.id,
            start_sec=seg_medium.start_sec,
            end_sec=seg_medium.end_sec,
            quote_text=seg_medium.text,
            status="candidate",
            source="flag",
        )
        db.session.add(quote_candidate)
        db.session.flush()

        needs_flag = SegmentFlag(segment_id=seg_needs.id, flag_type="needs_review")
        db.session.add(needs_flag)

        ai = AIAnalysis(
            project_id=project.id,
            interview_id=interview.id,
            question_id=question.id,
            analysis_type="integrated_interview_analysis",
            title="draft",
            summary_text="summary",
            content_json="{}",
            model_used="none",
            status="draft",
        )
        db.session.add(ai)
        db.session.flush()

        ignored_quote_item = ReviewItem(
            project_id=project.id,
            interview_id=interview.id,
            item_type="quote_candidate",
            target_type="quote_candidate",
            target_id=quote_candidate.id,
            severity="medium",
            status="ignored",
            reason="manually ignored",
        )
        db.session.add(ignored_quote_item)
        db.session.commit()

        summary1 = rebuild_review_items_for_interview(db.session, interview.id, resolve_missing=True)
        open_rows = ReviewItem.query.filter_by(interview_id=interview.id, status="open").all()
        open_counts = count_open_by_type(open_rows)

        failures += 0 if print_result(
            "unclassified_mapping generated",
            open_counts.get("unclassified_mapping", 0) == 1,
            f"count={open_counts.get('unclassified_mapping', 0)}",
        ) else 1
        failures += 0 if print_result(
            "low_confidence_mapping generated for medium+low",
            open_counts.get("low_confidence_mapping", 0) == 2,
            f"count={open_counts.get('low_confidence_mapping', 0)}",
        ) else 1
        failures += 0 if print_result(
            "unclassified does not duplicate into low_confidence",
            ReviewItem.query.filter_by(
                interview_id=interview.id,
                item_type="low_confidence_mapping",
                target_type="utterance_mapping",
                target_id=map_unclassified.id,
            ).count() == 0,
        ) else 1
        failures += 0 if print_result(
            "speaker_unassigned generated",
            open_counts.get("speaker_unassigned", 0) == 1,
            f"count={open_counts.get('speaker_unassigned', 0)}",
        ) else 1
        failures += 0 if print_result(
            "quote_candidate ignored does not auto-open",
            open_counts.get("quote_candidate", 0) == 0,
            f"open_count={open_counts.get('quote_candidate', 0)}",
        ) else 1
        failures += 0 if print_result(
            "needs_review_segment generated",
            open_counts.get("needs_review_segment", 0) == 1,
            f"count={open_counts.get('needs_review_segment', 0)}",
        ) else 1
        failures += 0 if print_result(
            "ai_analysis_draft generated",
            open_counts.get("ai_analysis_draft", 0) == 1,
            f"count={open_counts.get('ai_analysis_draft', 0)}",
        ) else 1

        summary2 = rebuild_review_items_for_interview(db.session, interview.id, resolve_missing=True)
        open_rows_second = ReviewItem.query.filter_by(interview_id=interview.id, status="open").all()
        failures += 0 if print_result(
            "second rebuild does not duplicate open items",
            len(open_rows_second) == len(open_rows),
            f"first={len(open_rows)}, second={len(open_rows_second)}",
        ) else 1
        failures += 0 if print_result(
            "summary existing grows on second rebuild",
            summary2.get("existing", 0) >= len(open_rows),
            f"existing={summary2.get('existing', 0)}",
        ) else 1

        map_unclassified.is_unclassified = False
        map_unclassified.confidence_level = "high"
        map_medium.confidence_level = "high"
        map_low.confidence_level = "high"
        speaker.participant_id = participant.id
        quote_candidate.status = "approved"
        needs_flag.flag_type = "quote"
        ai.status = "approved"
        db.session.commit()

        summary3 = rebuild_review_items_for_interview(db.session, interview.id, resolve_missing=True)
        open_after_clear = ReviewItem.query.filter_by(interview_id=interview.id, status="open").count()
        resolved_after_clear = ReviewItem.query.filter_by(interview_id=interview.id, status="resolved").count()

        failures += 0 if print_result(
            "resolve_missing closes inactive open items",
            open_after_clear == 0 and resolved_after_clear >= len(open_rows),
            f"open={open_after_clear}, resolved={resolved_after_clear}",
        ) else 1
        failures += 0 if print_result(
            "summary reports resolved count",
            summary3.get("resolved", 0) >= len(open_rows),
            f"resolved={summary3.get('resolved', 0)}",
        ) else 1

        quote_candidate.status = "candidate"
        db.session.commit()
        rebuild_review_items_for_interview(db.session, interview.id, resolve_missing=True)
        failures += 0 if print_result(
            "ignored stays ignored and is not reopened",
            ReviewItem.query.filter_by(
                interview_id=interview.id,
                item_type="quote_candidate",
                target_type="quote_candidate",
                target_id=quote_candidate.id,
                status="open",
            ).count() == 0 and ReviewItem.query.filter_by(
                interview_id=interview.id,
                item_type="quote_candidate",
                target_type="quote_candidate",
                target_id=quote_candidate.id,
                status="ignored",
            ).count() >= 1,
        ) else 1

        failures += 0 if print_result(
            "summary contains expected keys",
            set(summary1.keys()) == {"created", "existing", "resolved", "open_total"},
            f"keys={sorted(summary1.keys())}",
        ) else 1

    try:
        gs = subprocess.check_output(
            ["git", "status", "--short"],
            cwd=str(repo_root),
            text=True,
        ).strip()
        print(f"\n[INFO] git status --short:\n{gs if gs else '(clean)'}")
    except Exception as e:
        print(f"\n[WARN] git status check skipped: {type(e).__name__}: {e}")

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
