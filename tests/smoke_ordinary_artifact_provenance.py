from __future__ import annotations

import json
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
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_ordinary_artifact_provenance_") as tmp:
        root = Path(tmp)
        db_path = root / "ordinary-artifact.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.participant import Participant, ParticipantAttribute
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from models.segment_flag import SegmentFlag
            from models.speaker_assignment import SpeakerAssignment
            from services.ordinary_artifact_provenance import (
                ORDINARY_PROVENANCE_KEY,
                begin_ordinary_artifact_source_snapshot,
                ordinary_artifact_currentness,
            )
            from services.report_analysis import generate_analysis_csv
            from services.report_formatted import generate_formatted_sheet
            from services.report_verbatim import generate_verbatim

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Ordinary artifact provenance", client="Client")
                db.session.add(project)
                db.session.flush()

                flow = InterviewFlow(project_id=project.id, title="Main flow", version="1.0")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section 1", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Question 1",
                    is_key_question=True,
                    seq=1,
                )
                db.session.add(question)

                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant 1",
                )
                db.session.add(participant)
                db.session.flush()
                db.session.add(ParticipantAttribute(
                    participant_id=participant.id,
                    attribute_key="age",
                    attribute_value="30s",
                    display_order=1,
                ))

                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                    interviewer_name="Moderator",
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    start_sec=1.0,
                    end_sec=3.0,
                    text="original respondent statement",
                    seq=1,
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
                db.session.add(SegmentFlag(
                    segment_id=segment.id,
                    flag_type="quote",
                ))
                db.session.add(SpeakerAssignment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    participant_id=participant.id,
                ))
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                segment_id = int(segment.id)

                verbatim = generate_verbatim(interview_id)
                formatted = generate_formatted_sheet(project_id)
                analysis = generate_analysis_csv(project_id)
                generated = [verbatim, formatted, analysis]

                for item in generated:
                    params = json.loads(item.generation_params_json or "{}")
                    provenance = params.get(ORDINARY_PROVENANCE_KEY)
                    failures += check(
                        f"{item.file_type} stores source provenance and artifact hash",
                        isinstance(provenance, dict)
                        and bool(provenance.get("sha256"))
                        and len(str(params.get("artifact_sha256") or "")) == 64,
                        f"params={params}",
                    )
                    status = ordinary_artifact_currentness(item)
                    failures += check(
                        f"{item.file_type} is current immediately after generation",
                        status.provenance_present and status.current,
                        status.reason,
                    )

                begin_ordinary_artifact_source_snapshot(project_id)
                second_write_blocked = False
                con = sqlite3.connect(str(db_path), timeout=0.05)
                try:
                    try:
                        con.execute(
                            "UPDATE projects SET name=? WHERE id=?",
                            ("concurrent mutation", project_id),
                        )
                        con.commit()
                    except sqlite3.OperationalError as exc:
                        second_write_blocked = "locked" in str(exc).lower()
                        con.rollback()
                finally:
                    con.close()
                    db.session.rollback()
                failures += check(
                    "ordinary artifact source snapshot blocks concurrent SQLite writes",
                    second_write_blocked,
                )

                segment = db.session.get(Segment, segment_id)
                segment.text = "canonical statement changed after generation"
                db.session.commit()

                for item in generated:
                    db.session.expire_all()
                    item = db.session.get(type(item), int(item.id))
                    status = ordinary_artifact_currentness(item)
                    failures += check(
                        f"{item.file_type} becomes stale after canonical source mutation",
                        status.provenance_present and not status.current,
                        status.reason,
                    )
                    response = client.get(f"/api/outputs/{int(item.id)}/download")
                    failures += check(
                        f"stale {item.file_type} download fails closed",
                        response.status_code == 409,
                        f"status={response.status_code}",
                    )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "ordinary artifact provenance smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if app is not None:
                try:
                    from models import db
                    with app.app_context():
                        db.session.remove()
                        db.engine.dispose()
                except Exception:
                    pass
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
