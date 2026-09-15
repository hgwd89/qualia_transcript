from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
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

    with tempfile.TemporaryDirectory(prefix="qualia_aux_input_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'aux-input-fencing.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import InterviewFlow
            from models.participant import Participant
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment
            from models.segment_flag import SegmentFlag
            from models.speaker_assignment import SpeakerAssignment

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Aux input fencing smoke")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Main flow")
                participant = Participant(project_id=project.id, participant_code="P01")
                db.session.add_all([flow, participant])
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
                    text="response",
                    seq=1,
                )
                db.session.add(segment)
                db.session.flush()
                existing_flag = SegmentFlag(
                    segment_id=segment.id,
                    flag_type="favorite",
                    note="keep",
                )
                db.session.add(existing_flag)
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                participant_id = int(participant.id)
                segment_id = int(segment.id)

                active = ProcessingJob(
                    project_id=project_id,
                    interview_id=None,
                    question_id=None,
                    job_type="analyze_integrated",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(active)
                db.session.commit()
                active_id = int(active.id)

                response = client.post(
                    f"/api/interviews/{interview_id}/speakers/SPEAKER_01",
                    json={
                        "speaker_role": "moderator",
                        "participant_id": participant_id,
                        "note": "manual",
                    },
                )
                payload = response.get_json() or {}
                db.session.expire_all()
                assignment = SpeakerAssignment.query.filter_by(
                    interview_id=interview_id,
                    speaker_label="SPEAKER_01",
                ).first()
                failures += check(
                    "active integrated analysis blocks speaker assignment mutation",
                    response.status_code == 409
                    and active_id in (payload.get("active_job_ids") or [])
                    and assignment is None,
                    f"status={response.status_code} payload={payload}",
                )

                response = client.post(
                    f"/api/segments/{segment_id}/flags",
                    json={"flag_type": "exclude", "note": "manual"},
                )
                payload = response.get_json() or {}
                db.session.expire_all()
                blocked_flag = SegmentFlag.query.filter_by(
                    segment_id=segment_id,
                    flag_type="exclude",
                ).first()
                failures += check(
                    "active integrated analysis blocks flag creation",
                    response.status_code == 409
                    and active_id in (payload.get("active_job_ids") or [])
                    and blocked_flag is None,
                    f"status={response.status_code} payload={payload}",
                )

                response = client.delete(
                    f"/api/segments/{segment_id}/flags/favorite"
                )
                payload = response.get_json() or {}
                db.session.expire_all()
                favorite_after_block = SegmentFlag.query.filter_by(
                    segment_id=segment_id,
                    flag_type="favorite",
                ).first()
                failures += check(
                    "active integrated analysis blocks flag deletion",
                    response.status_code == 409
                    and active_id in (payload.get("active_job_ids") or [])
                    and favorite_after_block is not None,
                    f"status={response.status_code} payload={payload}",
                )

                active = db.session.get(ProcessingJob, active_id)
                active.status = "succeeded"
                active.worker_pid = None
                active.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                response = client.post(
                    f"/api/interviews/{interview_id}/speakers/SPEAKER_01",
                    json={
                        "speaker_role": "moderator",
                        "participant_id": participant_id,
                        "note": "manual",
                    },
                )
                db.session.expire_all()
                assignment = SpeakerAssignment.query.filter_by(
                    interview_id=interview_id,
                    speaker_label="SPEAKER_01",
                ).first()
                failures += check(
                    "speaker assignment mutation proceeds after reader is terminal",
                    response.status_code == 200
                    and assignment is not None
                    and assignment.speaker_role == "moderator"
                    and assignment.participant_id == participant_id,
                    f"status={response.status_code} body={response.get_json()}",
                )

                response = client.post(
                    f"/api/segments/{segment_id}/flags",
                    json={"flag_type": "exclude", "note": "manual"},
                )
                db.session.expire_all()
                exclude_after_terminal = SegmentFlag.query.filter_by(
                    segment_id=segment_id,
                    flag_type="exclude",
                ).first()
                failures += check(
                    "flag mutation proceeds after reader is terminal",
                    response.status_code == 200
                    and exclude_after_terminal is not None
                    and exclude_after_terminal.note == "manual",
                    f"status={response.status_code} body={response.get_json()}",
                )

                response = client.delete(
                    f"/api/segments/{segment_id}/flags/favorite"
                )
                db.session.expire_all()
                favorite_after_terminal = SegmentFlag.query.filter_by(
                    segment_id=segment_id,
                    flag_type="favorite",
                ).first()
                failures += check(
                    "flag deletion proceeds after reader is terminal",
                    response.status_code == 200
                    and (response.get_json() or {}).get("deleted") == 1
                    and favorite_after_terminal is None,
                    f"status={response.status_code} body={response.get_json()}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "interview auxiliary input fencing smoke",
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
