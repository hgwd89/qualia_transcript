import json
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
        from models.analysis import AIAnalysis
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
        import services.analyzer as analyzer
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()

        project = Project(name="Per Question Analyzer Trace Smoke")
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
        db.session.add(question)
        db.session.flush()

        interview = Interview(project_id=project.id, participant_id=participant.id, flow_id=flow.id)
        db.session.add(interview)
        db.session.flush()

        seg_quote = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="S_RESP_Q",
            speaker_role="respondent",
            start_sec=10.0,
            end_sec=11.0,
            text="approved quote evidence text",
            seq=1,
        )
        seg_regular = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="S_RESP_R",
            speaker_role="respondent",
            start_sec=20.0,
            end_sec=21.0,
            text="regular respondent evidence text",
            seq=2,
        )
        seg_moderator = Segment(
            interview_id=interview.id,
            speaker_label="S_MOD",
            speaker_role="moderator",
            start_sec=5.0,
            end_sec=6.0,
            text="moderator should not be traced",
            seq=3,
        )
        seg_needs_review = Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="S_RESP_N",
            speaker_role="respondent",
            start_sec=1.0,
            end_sec=2.0,
            text="needs review should not be traced by default",
            seq=4,
        )
        db.session.add_all([seg_quote, seg_regular, seg_moderator, seg_needs_review])
        db.session.flush()

        db.session.add_all([
            UtteranceMapping(segment_id=seg_quote.id, question_id=question.id, confidence=0.9, confidence_level="high"),
            UtteranceMapping(segment_id=seg_regular.id, question_id=question.id, confidence=0.9, confidence_level="high"),
            UtteranceMapping(segment_id=seg_moderator.id, question_id=question.id, confidence=0.9, confidence_level="high"),
            UtteranceMapping(segment_id=seg_needs_review.id, question_id=question.id, confidence=0.9, confidence_level="high"),
        ])
        db.session.add_all([
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_Q",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_R",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
            SpeakerAssignment(interview_id=interview.id, speaker_label="S_MOD", speaker_role="moderator"),
            SpeakerAssignment(
                interview_id=interview.id,
                speaker_label="S_RESP_N",
                speaker_role="respondent",
                participant_id=participant.id,
            ),
        ])
        db.session.add_all([
            SegmentFlag(segment_id=seg_quote.id, flag_type="quote"),
            SegmentFlag(segment_id=seg_needs_review.id, flag_type="needs_review"),
        ])
        db.session.add_all([
            QuoteCandidate(
                quote_id="QT-APPROVED-ANALYZER-001",
                project_id=project.id,
                interview_id=interview.id,
                segment_id=seg_quote.id,
                participant_id=participant.id,
                question_id=question.id,
                start_sec=seg_quote.start_sec,
                end_sec=seg_quote.end_sec,
                quote_text=seg_quote.text,
                status="approved",
                source="human",
            ),
            QuoteCandidate(
                quote_id="QT-CANDIDATE-ANALYZER-001",
                project_id=project.id,
                interview_id=interview.id,
                segment_id=seg_regular.id,
                participant_id=participant.id,
                question_id=question.id,
                start_sec=seg_regular.start_sec,
                end_sec=seg_regular.end_sec,
                quote_text=seg_regular.text,
                status="candidate",
                source="human",
            ),
        ])
        db.session.commit()

        original_texts = {segment.id: segment.text for segment in Segment.query.all()}
        analysis_count_before = AIAnalysis.query.count()
        api_call_count = 0

        def fake_call_structured(system, user, schema, schema_name=None):
            nonlocal api_call_count
            api_call_count += 1
            return {
                "findings": [
                    {
                        "point": "Local mocked finding",
                        "evidence_quote": "mocked quote kept for legacy compatibility",
                        "participant_codes": ["P01"],
                        "question_codes": ["WRONG"],
                        "confidence": "high",
                    }
                ],
                "implications": "Local mocked implication",
                "unresolved": "Local mocked unresolved",
            }

        original_call_structured = analyzer.call_structured
        analyzer.call_structured = fake_call_structured
        try:
            analysis = analyzer.analyze_per_question(interview.id, question.id)
        finally:
            analyzer.call_structured = original_call_structured

        db.session.refresh(analysis)
        content = json.loads(analysis.content_json)
        column_source_segment_ids = json.loads(analysis.source_segment_ids)
        column_quote_ids = json.loads(analysis.quote_ids)
        content_source_segment_ids = content.get("source_segment_ids")
        content_quotes = content.get("source_segment_quotes") or []
        content_quote_ids = content.get("quote_ids")

        failures += 0 if print_result(
            "analyze_per_question creates one AIAnalysis",
            AIAnalysis.query.count() == analysis_count_before + 1,
            f"before={analysis_count_before}, after={AIAnalysis.query.count()}",
        ) else 1
        failures += 0 if print_result(
            "analysis_type is per_question",
            analysis.analysis_type == "per_question",
            analysis.analysis_type,
        ) else 1
        failures += 0 if print_result(
            "mocked call_structured was used once",
            api_call_count == 1,
            f"api_call_count={api_call_count}",
        ) else 1
        failures += 0 if print_result(
            "content_json contains trace fields",
            isinstance(content_source_segment_ids, list)
            and isinstance(content_quotes, list)
            and isinstance(content_quote_ids, list),
        ) else 1
        failures += 0 if print_result(
            "AIAnalysis columns contain trace JSON",
            column_source_segment_ids == content_source_segment_ids and column_quote_ids == content_quote_ids,
            f"columns={column_source_segment_ids}/{column_quote_ids}, content={content_source_segment_ids}/{content_quote_ids}",
        ) else 1
        failures += 0 if print_result(
            "approved quote id is stored and candidate quote id is excluded",
            content_quote_ids == ["QT-APPROVED-ANALYZER-001"],
            f"quote_ids={content_quote_ids}",
        ) else 1
        failures += 0 if print_result(
            "trace includes respondent evidence only",
            seg_quote.id in content_source_segment_ids
            and seg_regular.id in content_source_segment_ids
            and seg_moderator.id not in content_source_segment_ids
            and seg_needs_review.id not in content_source_segment_ids,
            f"source_segment_ids={content_source_segment_ids}",
        ) else 1
        failures += 0 if print_result(
            "source_segment_quotes text comes from Segment.text",
            all(row["text"] == original_texts[row["segment_id"]] for row in content_quotes),
        ) else 1
        failures += 0 if print_result(
            "question code is normalized by app",
            content["findings"][0]["question_codes"] == ["Q1"],
            f"question_codes={content['findings'][0]['question_codes']}",
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
