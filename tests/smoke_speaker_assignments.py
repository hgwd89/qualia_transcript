import subprocess
import sys
import tempfile
from pathlib import Path


SEGMENT_TEXTS = {
    "RESP_A": "回答者の発話Aです。",
    "RESP_B": "回答者の発話Bです。",
    "MOD_A": "司会者の発話です。",
    "OBS_A": "観察者の発話です。",
}


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


def repo_state(repo_root: Path) -> dict[str, bool]:
    return {
        "instance": (repo_root / "instance").exists(),
        "uploads": (repo_root / "uploads").exists(),
        "outputs": (repo_root / "outputs").exists(),
    }


def create_fixture(db, Project, Participant, Interview, Segment) -> dict[str, int]:
    project = Project(name="Speaker Assignment Smoke Project", client="Smoke Client")
    db.session.add(project)
    db.session.flush()

    participant = Participant(
        project_id=project.id,
        participant_code="P01",
        display_name="Smoke Participant",
    )
    db.session.add(participant)
    db.session.flush()

    interview = Interview(
        project_id=project.id,
        participant_id=participant.id,
        status="mapped",
    )
    db.session.add(interview)
    db.session.flush()

    for index, (label, text) in enumerate(SEGMENT_TEXTS.items(), start=1):
        db.session.add(
            Segment(
                interview_id=interview.id,
                participant_id=participant.id if label.startswith("RESP") else None,
                speaker_label=label,
                speaker_role="unknown",
                start_sec=float(index),
                end_sec=float(index + 1),
                text=text,
                seq=index,
            )
        )
    db.session.commit()
    return {"project_id": project.id, "participant_id": participant.id, "interview_id": interview.id}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    before_repo_state = repo_state(repo_root)

    try:
        import config

        original_config = {
            "DATABASE_URI": config.DATABASE_URI,
            "UPLOAD_DIR": config.UPLOAD_DIR,
            "OUTPUT_DIR": config.OUTPUT_DIR,
        }
    except Exception as e:
        print_result("config import", False, f"{type(e).__name__}: {e}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        try:
            config.DATABASE_URI = f"sqlite:///{(tmp_dir / 'speaker_assignments_smoke.db').as_posix()}"
            config.UPLOAD_DIR = str(tmp_dir / "uploads")
            config.OUTPUT_DIR = str(tmp_dir / "outputs")

            from app import create_app
            from models import db
            from models.interview import Interview
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment
            from models.speaker_assignment import SpeakerAssignment

            app = create_app()
            client = app.test_client()

            failures += 0 if print_result(
                "temporary database configured",
                app.config.get("SQLALCHEMY_DATABASE_URI") == config.DATABASE_URI,
                app.config.get("SQLALCHEMY_DATABASE_URI", ""),
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_dir) in config.UPLOAD_DIR and str(tmp_dir) in config.OUTPUT_DIR,
            ) else 1

            with app.app_context():
                ids = create_fixture(db, Project, Participant, Interview, Segment)
                interview_id = ids["interview_id"]
                participant_id = ids["participant_id"]
                baseline_texts = {
                    s.speaker_label: s.text
                    for s in Segment.query.filter_by(interview_id=interview_id).all()
                }
                failures += 0 if print_result(
                    "self-contained interview fixture created",
                    len(baseline_texts) == len(SEGMENT_TEXTS),
                    f"interview_id={interview_id}",
                ) else 1

            r_page = client.get(f"/interviews/{interview_id}/speakers")
            failures += 0 if print_result(
                "GET /interviews/<id>/speakers",
                r_page.status_code == 200,
                f"status={r_page.status_code}",
            ) else 1

            r1 = client.post(
                f"/api/interviews/{interview_id}/speakers/RESP_A",
                json={
                    "speaker_role": "respondent",
                    "participant_id": participant_id,
                    "note": "__smoke_respondent__",
                },
            )
            j1 = r1.get_json(silent=True) or {}
            respondent_ok = (
                r1.status_code == 200
                and j1.get("ok") is True
                and (j1.get("assignment") or {}).get("speaker_role") == "respondent"
                and (j1.get("assignment") or {}).get("participant_id") == participant_id
            )
            failures += 0 if print_result(
                "upsert respondent assignment",
                respondent_ok,
                f"status={r1.status_code}, created={j1.get('created')}",
            ) else 1

            r2 = client.post(
                f"/api/interviews/{interview_id}/speakers/RESP_A",
                json={
                    "speaker_role": "respondent",
                    "participant_id": participant_id,
                    "note": "__smoke_respondent_updated__",
                },
            )
            j2 = r2.get_json(silent=True) or {}
            with app.app_context():
                respondent_count = SpeakerAssignment.query.filter_by(
                    interview_id=interview_id,
                    speaker_label="RESP_A",
                ).count()
            failures += 0 if print_result(
                "upsert duplicate prevented",
                r2.status_code == 200 and j2.get("ok") is True and respondent_count == 1,
                f"status={r2.status_code}, created={j2.get('created')}, count={respondent_count}",
            ) else 1

            r_mod = client.post(
                f"/api/interviews/{interview_id}/speakers/MOD_A",
                json={"speaker_role": "moderator", "participant_id": None, "note": "__smoke_moderator__"},
            )
            j_mod = r_mod.get_json(silent=True) or {}
            moderator_ok = (
                r_mod.status_code == 200
                and j_mod.get("ok") is True
                and (j_mod.get("assignment") or {}).get("speaker_role") == "moderator"
                and (j_mod.get("assignment") or {}).get("participant_id") is None
            )
            failures += 0 if print_result(
                "upsert moderator assignment without participant",
                moderator_ok,
                f"status={r_mod.status_code}",
            ) else 1

            r_obs = client.post(
                f"/api/interviews/{interview_id}/speakers/OBS_A",
                json={"speaker_role": "observer", "participant_id": None, "note": "__smoke_observer__"},
            )
            j_obs = r_obs.get_json(silent=True) or {}
            observer_ok = (
                r_obs.status_code == 200
                and j_obs.get("ok") is True
                and (j_obs.get("assignment") or {}).get("speaker_role") == "observer"
                and (j_obs.get("assignment") or {}).get("participant_id") is None
            )
            failures += 0 if print_result(
                "upsert observer assignment without participant",
                observer_ok,
                f"status={r_obs.status_code}",
            ) else 1

            with app.app_context():
                assignment_count = SpeakerAssignment.query.filter_by(interview_id=interview_id).count()
                final_texts = {
                    s.speaker_label: s.text
                    for s in Segment.query.filter_by(interview_id=interview_id).all()
                }
                db.session.remove()
                db.engine.dispose()

            failures += 0 if print_result(
                "speaker assignments created/updated",
                assignment_count == 3,
                f"count={assignment_count}",
            ) else 1
            failures += 0 if print_result(
                "segment text unchanged",
                final_texts == baseline_texts,
            ) else 1
        except Exception as e:
            failures += 0 if print_result("speaker assignment smoke", False, f"{type(e).__name__}: {e}") else 1
        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    after_repo_state = repo_state(repo_root)
    failures += 0 if print_result(
        "repo instance/uploads/outputs state unchanged",
        before_repo_state == after_repo_state,
        f"before={before_repo_state}, after={after_repo_state}",
    ) else 1

    status_proc = run_git(repo_root, "status", "--short")
    if status_proc.returncode == 0:
        out = status_proc.stdout.strip()
        print("git status --short:")
        print(out if out else "(clean)")
    else:
        failures += 0 if print_result(
            "git status --short",
            False,
            (status_proc.stderr or status_proc.stdout or "unknown git error").strip(),
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
