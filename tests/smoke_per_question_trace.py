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


def run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    try:
        from models import db
        from models.analysis import AIAnalysis  # noqa: F401 (mapper registry)
        from models.generated_file import GeneratedFile  # noqa: F401 (mapper registry)
        from models.interview import Interview, MediaFile, Transcription  # noqa: F401
        from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
        from models.participant import Participant, ParticipantAttribute  # noqa: F401
        from models.project import Project
        from models.quote_candidate import QuoteCandidate
        from models.review_item import ReviewItem  # noqa: F401 (mapper registry)
        from models.segment import Segment, UtteranceMapping
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment
        from services.analysis_trace import build_per_question_trace
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()

        project = Project(name="Per Question Trace Smoke")
        db.session.add(project)
        db.session.flush()

        participant = Participant(project_id=project.id, participant_code="P01", display_name="Trace User")
        db.session.add(participant)
        db.session.flush()

        flow = InterviewFlow(project_id=project.id, title="Trace Flow")
        db.session.add(flow)
        db.session.flush()

        section = InterviewFlowSection(flow_id=flow.id, title="Main", seq=1)
        db.session.add(section)
        db.session.flush()

        question = InterviewFlowQuestion(
            section_id=section.id,
            question_code="Q1",
            question_text="Why did you choose it?",
            seq=1,
        )
        other_question = InterviewFlowQuestion(
            section_id=section.id,
            question_code="Q2",
            question_text="Other question",
            seq=2,
        )
        db.session.add_all([question, other_question])
        db.session.flush()

        interview = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id)
        db.session.add(interview)
        db.session.flush()

        def make_segment(label: str, text: str, seq: int, start: float, role: str = "respondent") -> Segment:
            segment = Segment(
                interview_id=interview.id,
                participant_id=participant.id if role == "respondent" else None,
                speaker_label=label,
                speaker_role=role,
                start_sec=start,
                end_sec=start + 1.0,
                text=text,
                seq=seq,
            )
            db.session.add(segment)
            return segment

        seg_regular = make_segment("S_RESP_A", "regular respondent text", 1, 20.0)
        seg_quote_flag = make_segment("S_RESP_B", "quote flag respondent text", 2, 30.0)
        seg_approved_quote = make_segment("S_RESP_C", "approved quote respondent text", 3, 10.0)
        seg_candidate_quote = make_segment("S_RESP_D", "candidate quote respondent text", 4, 40.0)
        seg_needs_review = make_segment("S_RESP_E", "needs review respondent text", 5, 5.0)
        seg_exclude = make_segment("S_RESP_F", "excluded respondent text", 6, 6.0)
        seg_moderator = make_segment("S_MOD", "moderator text", 7, 7.0, role="moderator")
        seg_observer = make_segment("S_OBS", "observer text", 8, 8.0, role="observer")
        seg_missing_assignment = make_segment("S_MISSING", "missing assignment text", 9, 9.0)
        seg_other_question = make_segment("S_OTHER_Q", "other question text", 10, 1.0)
        db.session.flush()

        mapped_segments = [
            seg_regular,
            seg_quote_flag,
            seg_approved_quote,
            seg_candidate_quote,
            seg_needs_review,
            seg_exclude,
            seg_moderator,
            seg_observer,
            seg_missing_assignment,
        ]
        db.session.add_all([
            UtteranceMapping(segment_id=segment.id, question_id=question.id, confidence=0.9, confidence_level="high")
            for segment in mapped_segments
        ])
        db.session.add(UtteranceMapping(segment_id=seg_other_question.id, question_id=other_question.id))

        db.session.add_all([
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_A",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_B",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_C",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_D",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_E",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_F",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(interview_id=interview.id, speaker_label="S_MOD", speaker_role="moderator"),
            SpeakerAssignment(interview_id=interview.id, speaker_label="S_OBS", speaker_role="observer"),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_OTHER_Q",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
        ])

        db.session.add_all([
            SegmentFlag(segment_id=seg_quote_flag.id, flag_type="quote"),
            SegmentFlag(segment_id=seg_needs_review.id, flag_type="needs_review"),
            SegmentFlag(segment_id=seg_exclude.id, flag_type="exclude"),
        ])

        db.session.add_all([
            QuoteCandidate(
                quote_id="QT-APPROVED-001",
                project_id=project.id,
                interview_id=interview.id,
                segment_id=seg_approved_quote.id,
                participant_id=participant.id,
                question_id=question.id,
                start_sec=seg_approved_quote.start_sec,
                end_sec=seg_approved_quote.end_sec,
                quote_text=seg_approved_quote.text,
                status="approved",
                source="human",
            ),
            QuoteCandidate(
                quote_id="QT-CANDIDATE-001",
                project_id=project.id,
                interview_id=interview.id,
                segment_id=seg_candidate_quote.id,
                participant_id=participant.id,
                question_id=question.id,
                start_sec=seg_candidate_quote.start_sec,
                end_sec=seg_candidate_quote.end_sec,
                quote_text=seg_candidate_quote.text,
                status="candidate",
                source="human",
            ),
            QuoteCandidate(
                quote_id="QT-REJECTED-001",
                project_id=project.id,
                interview_id=interview.id,
                segment_id=seg_candidate_quote.id,
                participant_id=participant.id,
                question_id=question.id,
                start_sec=seg_candidate_quote.start_sec,
                end_sec=seg_candidate_quote.end_sec,
                quote_text=seg_candidate_quote.text,
                status="rejected",
                source="human",
            ),
        ])
        db.session.commit()

        original_texts = {segment.id: segment.text for segment in Segment.query.all()}
        counts_before = {
            "segments": Segment.query.count(),
            "mappings": UtteranceMapping.query.count(),
            "flags": SegmentFlag.query.count(),
            "quotes": QuoteCandidate.query.count(),
        }

        trace = build_per_question_trace(db.session, interview.id, question.id)
        source_ids = trace["source_segment_ids"]
        quotes_by_id = {row["segment_id"]: row for row in trace["source_segment_quotes"]}

        failures += 0 if print_result(
            "respondent mapped segments are included",
            seg_regular.id in source_ids and seg_candidate_quote.id in source_ids,
            f"source_ids={source_ids}",
        ) else 1
        failures += 0 if print_result(
            "moderator and observer are excluded",
            seg_moderator.id not in source_ids and seg_observer.id not in source_ids,
            f"source_ids={source_ids}",
        ) else 1
        failures += 0 if print_result(
            "missing speaker assignment is excluded",
            seg_missing_assignment.id not in source_ids,
            f"source_ids={source_ids}",
        ) else 1
        failures += 0 if print_result(
            "exclude and needs_review are excluded by default",
            seg_exclude.id not in source_ids and seg_needs_review.id not in source_ids,
            f"source_ids={source_ids}",
        ) else 1

        trace_with_needs = build_per_question_trace(
            db.session,
            interview.id,
            question.id,
            include_needs_review=True,
        )
        failures += 0 if print_result(
            "include_needs_review includes needs_review segment",
            seg_needs_review.id in trace_with_needs["source_segment_ids"]
            and seg_exclude.id not in trace_with_needs["source_segment_ids"],
            f"source_ids={trace_with_needs['source_segment_ids']}",
        ) else 1

        failures += 0 if print_result(
            "quote flag and approved quote segments are prioritized",
            source_ids[:2] == [seg_approved_quote.id, seg_quote_flag.id],
            f"first_ids={source_ids[:3]}",
        ) else 1
        failures += 0 if print_result(
            "approved quote_id is returned",
            trace["quote_ids"] == ["QT-APPROVED-001"],
            f"quote_ids={trace['quote_ids']}",
        ) else 1
        failures += 0 if print_result(
            "candidate/rejected quote_ids are excluded",
            "QT-CANDIDATE-001" not in trace["quote_ids"] and "QT-REJECTED-001" not in trace["quote_ids"],
            f"quote_ids={trace['quote_ids']}",
        ) else 1
        failures += 0 if print_result(
            "source_segment_quotes text is Segment.text",
            all(row["text"] == original_texts[row["segment_id"]] for row in trace["source_segment_quotes"]),
        ) else 1
        failures += 0 if print_result(
            "source_segment_quotes include expected fields",
            all(
                {"segment_id", "text", "speaker_label", "participant_id", "start_sec", "end_sec"}.issubset(row.keys())
                for row in trace["source_segment_quotes"]
            ),
        ) else 1
        failures += 0 if print_result(
            "participant_id comes from respondent assignment",
            quotes_by_id[seg_regular.id]["participant_id"] == participant.id,
            f"participant_id={quotes_by_id[seg_regular.id]['participant_id']}",
        ) else 1

        limited_trace = build_per_question_trace(db.session, interview.id, question.id, max_segments=2)
        failures += 0 if print_result(
            "max_segments limits after priority sort",
            limited_trace["source_segment_ids"] == source_ids[:2],
            f"limited={limited_trace['source_segment_ids']}, expected={source_ids[:2]}",
        ) else 1

        empty_trace = build_per_question_trace(db.session, interview.id, 999999)
        failures += 0 if print_result(
            "empty trace returns empty lists",
            empty_trace == {"source_segment_ids": [], "source_segment_quotes": [], "quote_ids": []},
            f"empty_trace={empty_trace}",
        ) else 1

        counts_after = {
            "segments": Segment.query.count(),
            "mappings": UtteranceMapping.query.count(),
            "flags": SegmentFlag.query.count(),
            "quotes": QuoteCandidate.query.count(),
        }
        failures += 0 if print_result(
            "helper does not update DB rows",
            counts_before == counts_after,
            f"before={counts_before}, after={counts_after}",
        ) else 1
        failures += 0 if print_result(
            "Segment.text is unchanged",
            all(db.session.get(Segment, segment_id).text == text for segment_id, text in original_texts.items()),
        ) else 1

        db.session.remove()
        db.engine.dispose()

    status_proc = run_git(repo_root, "status", "--short")
    if status_proc.returncode == 0:
        out = status_proc.stdout.strip()
        print("git status --short:")
        print(out if out else "(clean)")

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
