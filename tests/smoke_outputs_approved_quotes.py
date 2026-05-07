import sys
import tempfile
from pathlib import Path

from docx import Document
from flask import Flask


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def _section_text(paragraph_texts: list[str], heading: str) -> str:
    try:
        start_idx = paragraph_texts.index(heading)
    except ValueError:
        return ""
    return "\n".join(paragraph_texts[start_idx + 1 :])


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    try:
        import config
        from models import db
        from models.analysis import AIAnalysis  # noqa: F401 (mapper registry)
        from models.generated_file import GeneratedFile
        from models.interview import Interview, MediaFile, Transcription  # noqa: F401
        from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
        from models.participant import Participant, ParticipantAttribute  # noqa: F401
        from models.project import Project
        from models.quote_candidate import QuoteCandidate
        from models.review_item import ReviewItem  # noqa: F401
        from models.segment import Segment, UtteranceMapping
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment  # noqa: F401
        from services.report_verbatim import generate_verbatim
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context(), tempfile.TemporaryDirectory() as tmp_output_dir:
        old_output_dir = config.OUTPUT_DIR
        config.OUTPUT_DIR = tmp_output_dir
        try:
            db.create_all()

            project = Project(name="Approved Quote Output Smoke")
            db.session.add(project)
            db.session.flush()

            participant = Participant(project_id=project.id, participant_code="P01", display_name="User")
            db.session.add(participant)
            db.session.flush()

            flow = InterviewFlow(project_id=project.id, title="Flow")
            db.session.add(flow)
            db.session.flush()

            section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
            db.session.add(section)
            db.session.flush()

            question = InterviewFlowQuestion(section_id=section.id, question_code="Q1", question_text="Q1", seq=1)
            db.session.add(question)
            db.session.flush()

            interview = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id, status="mapped")
            db.session.add(interview)
            db.session.flush()

            seg_approved = Segment(
                interview_id=interview.id,
                participant_id=participant.id,
                speaker_label="SPEAKER_00",
                speaker_role="respondent",
                start_sec=1.0,
                end_sec=4.0,
                text="APPROVED_FORMAL_QUOTE_TEXT",
                seq=1,
            )
            seg_candidate = Segment(
                interview_id=interview.id,
                participant_id=participant.id,
                speaker_label="SPEAKER_01",
                speaker_role="respondent",
                start_sec=4.0,
                end_sec=6.0,
                text="CANDIDATE_NOT_FORMAL_TEXT",
                seq=2,
            )
            seg_rejected = Segment(
                interview_id=interview.id,
                participant_id=participant.id,
                speaker_label="SPEAKER_02",
                speaker_role="respondent",
                start_sec=6.0,
                end_sec=8.0,
                text="REJECTED_NOT_FORMAL_TEXT",
                seq=3,
            )
            db.session.add_all([seg_approved, seg_candidate, seg_rejected])
            db.session.flush()

            baseline_texts = {
                seg_approved.id: seg_approved.text,
                seg_candidate.id: seg_candidate.text,
                seg_rejected.id: seg_rejected.text,
            }

            db.session.add_all(
                [
                    UtteranceMapping(
                        segment_id=seg_approved.id,
                        question_id=question.id,
                        mapped_by="ai",
                        confidence=0.95,
                        confidence_level="high",
                        is_unclassified=False,
                    ),
                    UtteranceMapping(
                        segment_id=seg_candidate.id,
                        question_id=question.id,
                        mapped_by="ai",
                        confidence=0.70,
                        confidence_level="medium",
                        is_unclassified=False,
                    ),
                    UtteranceMapping(
                        segment_id=seg_rejected.id,
                        question_id=question.id,
                        mapped_by="ai",
                        confidence=0.60,
                        confidence_level="medium",
                        is_unclassified=False,
                    ),
                ]
            )
            db.session.add(SegmentFlag(segment_id=seg_candidate.id, flag_type="quote"))

            db.session.add_all(
                [
                    QuoteCandidate(
                        quote_id=f"QT-{interview.id}-{seg_approved.id}-APP",
                        project_id=project.id,
                        interview_id=interview.id,
                        segment_id=seg_approved.id,
                        participant_id=participant.id,
                        question_id=question.id,
                        start_sec=seg_approved.start_sec,
                        end_sec=seg_approved.end_sec,
                        quote_text=seg_approved.text,
                        status="approved",
                        source="human",
                    ),
                    QuoteCandidate(
                        quote_id=f"QT-{interview.id}-{seg_candidate.id}-CAN",
                        project_id=project.id,
                        interview_id=interview.id,
                        segment_id=seg_candidate.id,
                        participant_id=participant.id,
                        question_id=question.id,
                        start_sec=seg_candidate.start_sec,
                        end_sec=seg_candidate.end_sec,
                        quote_text=seg_candidate.text,
                        status="candidate",
                        source="flag",
                    ),
                    QuoteCandidate(
                        quote_id=f"QT-{interview.id}-{seg_rejected.id}-REJ",
                        project_id=project.id,
                        interview_id=interview.id,
                        segment_id=seg_rejected.id,
                        participant_id=participant.id,
                        question_id=question.id,
                        start_sec=seg_rejected.start_sec,
                        end_sec=seg_rejected.end_sec,
                        quote_text=seg_rejected.text,
                        status="rejected",
                        source="human",
                    ),
                ]
            )
            db.session.commit()

            gf = generate_verbatim(interview.id)
            doc_path = Path(config.OUTPUT_DIR) / gf.stored_path
            failures += 0 if print_result(
                "docx generated under temp output dir",
                doc_path.is_file() and str(doc_path).startswith(str(Path(tmp_output_dir))),
                str(doc_path),
            ) else 1

            doc = Document(str(doc_path))
            texts = [p.text for p in doc.paragraphs]

            formal_heading = "【正式引用（承認済み）】"
            failures += 0 if print_result(
                "formal approved quote section exists",
                formal_heading in texts,
            ) else 1

            formal_text = _section_text(texts, formal_heading)
            failures += 0 if print_result(
                "approved quote appears in formal section",
                "APPROVED_FORMAL_QUOTE_TEXT" in formal_text,
            ) else 1
            failures += 0 if print_result(
                "candidate quote is excluded from formal section",
                "CANDIDATE_NOT_FORMAL_TEXT" not in formal_text,
            ) else 1
            failures += 0 if print_result(
                "rejected quote is excluded from formal section",
                "REJECTED_NOT_FORMAL_TEXT" not in formal_text,
            ) else 1

            failures += 0 if print_result(
                "existing star marker is preserved",
                any("★引用候補" in t for t in texts),
            ) else 1

            post_texts = {s.id: s.text for s in Segment.query.order_by(Segment.id.asc()).all()}
            failures += 0 if print_result(
                "Segment.text remains unchanged",
                post_texts == baseline_texts,
            ) else 1
        finally:
            config.OUTPUT_DIR = old_output_dir

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())

