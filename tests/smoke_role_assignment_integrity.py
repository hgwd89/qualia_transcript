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

    with tempfile.TemporaryDirectory(prefix="qualia_role_assignment_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'role_assignment.db').as_posix()}"
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
            from models.speaker_assignment import SpeakerAssignment
            from services.report_formatted import (
                _effective_speaker_role,
                _respondent_mappings,
                _respondent_unclassified_segments,
                _speaker_assignment_maps,
            )

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Role Assignment Integrity")
                db.session.add(project)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Respondent",
                )
                db.session.add(participant)
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
                    question_text="Question",
                    question_type="open",
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()

                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                )
                db.session.add(interview)
                db.session.flush()

                role_segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="ROLE_EDIT",
                    speaker_role="respondent",
                    text="role edit source text",
                    seq=1,
                )
                assigned_respondent = Segment(
                    interview_id=interview.id,
                    speaker_label="ASSIGNED_RESP",
                    speaker_role="unknown",
                    text="assigned respondent text",
                    seq=2,
                )
                assigned_moderator = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="ASSIGNED_MOD",
                    speaker_role="respondent",
                    text="assigned moderator text",
                    seq=3,
                )
                raw_respondent = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="RAW_RESP",
                    speaker_role="respondent",
                    text="raw respondent text",
                    seq=4,
                )
                assigned_unmapped = Segment(
                    interview_id=interview.id,
                    speaker_label="ASSIGNED_UNMAPPED",
                    speaker_role="unknown",
                    text="assigned zero-mapping respondent text",
                    seq=5,
                )
                db.session.add_all([
                    role_segment,
                    assigned_respondent,
                    assigned_moderator,
                    raw_respondent,
                    assigned_unmapped,
                ])
                db.session.flush()

                mapping_assigned = UtteranceMapping(
                    segment_id=assigned_respondent.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                )
                mapping_moderator = UtteranceMapping(
                    segment_id=assigned_moderator.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                )
                mapping_raw = UtteranceMapping(
                    segment_id=raw_respondent.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add_all([
                    mapping_assigned,
                    mapping_moderator,
                    mapping_raw,
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="ASSIGNED_RESP",
                        speaker_role="respondent",
                        participant_id=participant.id,
                    ),
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="ASSIGNED_MOD",
                        speaker_role="moderator",
                    ),
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="ASSIGNED_UNMAPPED",
                        speaker_role="respondent",
                        participant_id=participant.id,
                    ),
                ])
                db.session.commit()

                ids = {
                    "interview": interview.id,
                    "participant": participant.id,
                    "role_segment": role_segment.id,
                    "question": question.id,
                    "assigned_mapping": mapping_assigned.id,
                    "moderator_mapping": mapping_moderator.id,
                    "raw_mapping": mapping_raw.id,
                    "assigned_unmapped": assigned_unmapped.id,
                }
                baseline_text = role_segment.text

            role_url = (
                f"/interviews/{ids['interview']}/segments/"
                f"{ids['role_segment']}/role"
            )

            malformed = client.post(
                role_url,
                data="{",
                content_type="application/json",
            )
            failures += check(
                "malformed JSON role update is rejected",
                malformed.status_code == 400,
                str(malformed.status_code),
            )

            empty = client.post(role_url, json={})
            failures += check(
                "empty JSON role update is rejected",
                empty.status_code == 400,
                str(empty.status_code),
            )

            incomplete = client.post(
                role_url,
                json={"speaker_role": "observer"},
            )
            failures += check(
                "role update missing participant_id is rejected",
                incomplete.status_code == 400,
                str(incomplete.status_code),
            )

            with app.app_context():
                seg = db.session.get(Segment, ids["role_segment"])
                failures += check(
                    "rejected role updates preserve participant and role",
                    seg.participant_id == ids["participant"]
                    and seg.speaker_role == "respondent",
                    f"participant={seg.participant_id} role={seg.speaker_role}",
                )
                failures += check(
                    "rejected role updates preserve Segment.text",
                    seg.text == baseline_text,
                )

            valid = client.post(
                role_url,
                json={
                    "speaker_role": "observer",
                    "participant_id": ids["participant"],
                },
            )
            failures += check(
                "complete role update remains accepted",
                valid.status_code == 200,
                str(valid.status_code),
            )

            explicit_clear = client.post(
                role_url,
                json={"speaker_role": "observer", "participant_id": None},
            )
            failures += check(
                "explicit participant clear remains supported",
                explicit_clear.status_code == 200,
                str(explicit_clear.status_code),
            )

            with app.app_context():
                interview = db.session.get(Interview, ids["interview"])
                assignment_maps = _speaker_assignment_maps([interview])
                assignment_map = assignment_maps[ids["interview"]]
                mapped = _respondent_mappings(
                    ids["question"],
                    ids["interview"],
                    assignment_map,
                )
                mapping_ids = {item.id for item in mapped}
                unclassified = _respondent_unclassified_segments(
                    interview,
                    assignment_map,
                )
                unclassified_ids = {item.id for item in unclassified}

                assigned_seg = (
                    Segment.query
                    .filter_by(interview_id=ids["interview"], speaker_label="ASSIGNED_RESP")
                    .one()
                )
                moderator_seg = (
                    Segment.query
                    .filter_by(interview_id=ids["interview"], speaker_label="ASSIGNED_MOD")
                    .one()
                )
                failures += check(
                    "SpeakerAssignment respondent overrides unknown Segment role",
                    _effective_speaker_role(assigned_seg, assignment_map) == "respondent"
                    and ids["assigned_mapping"] in mapping_ids,
                    str(mapping_ids),
                )
                failures += check(
                    "SpeakerAssignment moderator overrides respondent Segment role",
                    _effective_speaker_role(moderator_seg, assignment_map) == "moderator"
                    and ids["moderator_mapping"] not in mapping_ids,
                    str(mapping_ids),
                )
                failures += check(
                    "raw respondent remains included without assignment",
                    ids["raw_mapping"] in mapping_ids,
                    str(mapping_ids),
                )
                failures += check(
                    "zero-mapping assigned respondent is retained for unclassified output",
                    ids["assigned_unmapped"] in unclassified_ids,
                    str(unclassified_ids),
                )

                role_seg = db.session.get(Segment, ids["role_segment"])
                failures += check(
                    "accepted role updates still preserve Segment.text",
                    role_seg.text == baseline_text,
                )
                db.session.remove()
                db.engine.dispose()

        except Exception as exc:
            failures += check(
                "role/assignment integrity smoke",
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
