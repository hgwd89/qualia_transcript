import sqlite3
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
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_provenance_parity_") as tmp:
        root = Path(tmp)
        db_path = root / "provenance_parity.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.analysis_source_provenance import capture_analysis_source_provenance
            from services.analysis_source_provenance_sqlite import validate_analysis_source_provenance

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(
                    name="Provenance parity",
                    client="Parity Client",
                    research_objective="Compare ORM and read-only SQLite source manifests",
                )
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="重要なことは？",
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Parity Participant",
                )
                db.session.add(participant)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="安心感が重要です。",
                    seq=0,
                )
                db.session.add(segment)
                db.session.flush()
                db.session.add(UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                ))
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                question_id = int(question.id)
                expected = {
                    "per_question": capture_analysis_source_provenance(
                        "per_question",
                        project_id,
                        interview_id=interview_id,
                        question_id=question_id,
                    ),
                    "per_participant": capture_analysis_source_provenance(
                        "per_participant",
                        project_id,
                        interview_id=interview_id,
                    ),
                    "cross_participant": capture_analysis_source_provenance(
                        "cross_participant",
                        project_id,
                        question_id=question_id,
                    ),
                    "integrated": capture_analysis_source_provenance(
                        "integrated",
                        project_id,
                    ),
                }

                db.session.remove()
                db.engine.dispose()

            con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                scopes = {
                    "per_question": (interview_id, question_id),
                    "per_participant": (interview_id, None),
                    "cross_participant": (None, question_id),
                    "integrated": (None, None),
                }
                for analysis_type, provenance in expected.items():
                    interview_scope, question_scope = scopes[analysis_type]
                    ok, reason = validate_analysis_source_provenance(
                        con,
                        analysis_type=analysis_type,
                        project_id=project_id,
                        interview_id=interview_scope,
                        question_id=question_scope,
                        content={"source_provenance": provenance},
                    )
                    failures += check(
                        f"SQLite readiness fingerprint matches ORM {analysis_type}",
                        ok,
                        reason,
                    )
            finally:
                con.close()
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
