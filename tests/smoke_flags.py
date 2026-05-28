import subprocess
import sys
import tempfile
from pathlib import Path


FLAG_TYPES = ("favorite", "quote", "exclude", "needs_review")
SEGMENT_TEXT = "これはSegmentFlag smoke用の発話です。"


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


def create_fixture(db, Project, Participant, Interview, Segment) -> int:
    project = Project(name="SegmentFlag Smoke Project", client="Smoke Client")
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

    segment = Segment(
        interview_id=interview.id,
        participant_id=participant.id,
        speaker_label="SPEAKER_00",
        speaker_role="respondent",
        start_sec=1.0,
        end_sec=3.0,
        text=SEGMENT_TEXT,
        seq=1,
    )
    db.session.add(segment)
    db.session.commit()
    return segment.id


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
            config.DATABASE_URI = f"sqlite:///{(tmp_dir / 'flags_smoke.db').as_posix()}"
            config.UPLOAD_DIR = str(tmp_dir / "uploads")
            config.OUTPUT_DIR = str(tmp_dir / "outputs")

            from app import create_app
            from models import db
            from models.interview import Interview
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment
            from models.segment_flag import SegmentFlag

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
                segment_id = create_fixture(db, Project, Participant, Interview, Segment)
                seg = db.session.get(Segment, segment_id)
                baseline_text = seg.text
                baseline_flags = SegmentFlag.query.filter_by(segment_id=segment_id).count()
                failures += 0 if print_result(
                    "self-contained segment fixture created",
                    baseline_flags == 0 and baseline_text == SEGMENT_TEXT,
                    f"segment_id={segment_id}",
                ) else 1

            quote_note = "__smoke_quote__"
            r1 = client.post(
                f"/api/segments/{segment_id}/flags",
                json={"flag_type": "quote", "note": quote_note},
            )
            j1 = r1.get_json(silent=True) or {}
            failures += 0 if print_result(
                "create quote flag",
                r1.status_code == 200 and j1.get("ok") is True,
                f"status={r1.status_code}, created={j1.get('created')}",
            ) else 1

            with app.app_context():
                before_dup = SegmentFlag.query.filter_by(segment_id=segment_id, flag_type="quote").count()
            r2 = client.post(
                f"/api/segments/{segment_id}/flags",
                json={"flag_type": "quote", "note": quote_note},
            )
            j2 = r2.get_json(silent=True) or {}
            with app.app_context():
                after_dup = SegmentFlag.query.filter_by(segment_id=segment_id, flag_type="quote").count()
            failures += 0 if print_result(
                "duplicate quote prevented",
                r2.status_code == 200 and j2.get("ok") is True and before_dup == after_dup == 1,
                f"status={r2.status_code}, created={j2.get('created')}, count={after_dup}",
            ) else 1

            for flag_type in ("favorite", "exclude", "needs_review"):
                r = client.post(
                    f"/api/segments/{segment_id}/flags",
                    json={"flag_type": flag_type, "note": f"__smoke_{flag_type}__"},
                )
                j = r.get_json(silent=True) or {}
                failures += 0 if print_result(
                    f"create {flag_type} flag",
                    r.status_code == 200 and j.get("ok") is True,
                    f"status={r.status_code}, created={j.get('created')}",
                ) else 1

            with app.app_context():
                final_flag_types = sorted(
                    f.flag_type for f in SegmentFlag.query.filter_by(segment_id=segment_id).all()
                )
                seg = db.session.get(Segment, segment_id)
                final_text = seg.text
            failures += 0 if print_result(
                "all flag types created",
                final_flag_types == sorted(FLAG_TYPES),
                ", ".join(final_flag_types),
            ) else 1

            for flag_type in FLAG_TYPES:
                rd = client.delete(f"/api/segments/{segment_id}/flags/{flag_type}")
                jd = rd.get_json(silent=True) or {}
                failures += 0 if print_result(
                    f"delete {flag_type} flag",
                    rd.status_code == 200 and jd.get("ok") is True,
                    f"status={rd.status_code}, deleted={jd.get('deleted')}",
                ) else 1

            with app.app_context():
                remaining_flags = SegmentFlag.query.filter_by(segment_id=segment_id).count()
                seg = db.session.get(Segment, segment_id)
                text_after_delete = seg.text
                db.session.remove()
                db.engine.dispose()

            failures += 0 if print_result(
                "flags deleted",
                remaining_flags == 0,
                f"remaining={remaining_flags}",
            ) else 1
            failures += 0 if print_result(
                "segment text unchanged",
                final_text == baseline_text == text_after_delete,
            ) else 1
        except Exception as e:
            failures += 0 if print_result("segment flag smoke", False, f"{type(e).__name__}: {e}") else 1
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
