import subprocess
import sys
import tempfile
from pathlib import Path


LABEL_RESPONDENT_1 = "SMOKE_RESPONDENT_1"
LABEL_RESPONDENT_2 = "SMOKE_RESPONDENT_2"
LABEL_MODERATOR = "SMOKE_MODERATOR"
LABEL_OBSERVER = "SMOKE_OBSERVER"
TARGET_LABELS = (
    LABEL_RESPONDENT_1,
    LABEL_RESPONDENT_2,
    LABEL_MODERATOR,
    LABEL_OBSERVER,
)


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


def _repo_path_state(repo_root: Path) -> dict[str, bool]:
    return {name: (repo_root / name).exists() for name in ("instance", "uploads", "outputs")}


def _post_assignment(client, interview_id: int, label: str, role: str, participant_id, note: str):
    return client.post(
        f"/api/interviews/{interview_id}/speakers/{label}",
        json={
            "speaker_role": role,
            "participant_id": participant_id,
            "note": note,
        },
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    repo_path_state_before = _repo_path_state(repo_root)

    try:
        import config
        from app import create_app
        from models import db

        # Import related models so SQLAlchemy can resolve relationship strings.
        from models.analysis import AIAnalysis  # noqa: F401
        from models.api_usage_log import APIUsageLog  # noqa: F401
        from models.generated_file import GeneratedFile  # noqa: F401
        from models.interview import Interview
        from models.interview_flow import InterviewFlow  # noqa: F401
        from models.participant import Participant
        from models.project import Project
        from models.quote_candidate import QuoteCandidate  # noqa: F401
        from models.review_item import ReviewItem  # noqa: F401
        from models.segment import Segment
        from models.segment_flag import SegmentFlag  # noqa: F401
        from models.speaker_assignment import SpeakerAssignment
    except Exception as e:
        print_result("import app/models", False, f"{type(e).__name__}: {e}")
        return 1

    original_database_uri = config.DATABASE_URI
    original_upload_dir = config.UPLOAD_DIR
    original_output_dir = config.OUTPUT_DIR

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        config.DATABASE_URI = f"sqlite:///{(tmp_root / 'speaker_assignments_smoke.db').as_posix()}"
        config.UPLOAD_DIR = str(tmp_root / "uploads")
        config.OUTPUT_DIR = str(tmp_root / "outputs")

        try:
            app = create_app()
            client = app.test_client()

            failures += 0 if print_result(
                "temporary database configured",
                config.DATABASE_URI.startswith("sqlite:///")
                and config.DATABASE_URI.endswith("/speaker_assignments_smoke.db"),
                config.DATABASE_URI,
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_root) in config.UPLOAD_DIR and str(tmp_root) in config.OUTPUT_DIR,
            ) else 1

            with app.app_context():
                project = Project(name="Speaker Assignment Smoke")
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

                segments = []
                for seq, label in enumerate(TARGET_LABELS, start=1):
                    segments.append(
                        Segment(
                            interview_id=interview.id,
                            speaker_label=label,
                            speaker_role="unknown",
                            start_sec=float(seq * 10),
                            end_sec=float(seq * 10 + 5),
                            text=f"Speaker assignment smoke text for {label}",
                            seq=seq,
                        )
                    )
                db.session.add_all(segments)
                db.session.commit()

                interview_id = interview.id
                participant_id = participant.id
                baseline_segment_text = {seg.id: seg.text for seg in segments}

            failures += 0 if print_result(
                "temporary speaker fixture created",
                bool(interview_id and participant_id and len(baseline_segment_text) == len(TARGET_LABELS)),
                f"interview_id={interview_id}, labels={list(TARGET_LABELS)}",
            ) else 1

            page = client.get(f"/interviews/{interview_id}/speakers")
            failures += 0 if print_result(
                "GET /interviews/<id>/speakers",
                page.status_code == 200,
                f"status={page.status_code}",
            ) else 1

            r1 = _post_assignment(
                client,
                interview_id,
                LABEL_RESPONDENT_1,
                "respondent",
                participant_id,
                "__smoke_respondent_1__",
            )
            j1 = r1.get_json(silent=True) or {}
            a1 = j1.get("assignment") or {}
            failures += 0 if print_result(
                "respondent saves participant_id",
                r1.status_code == 200 and j1.get("ok") is True and a1.get("participant_id") == participant_id,
                f"status={r1.status_code}, participant_id={a1.get('participant_id')}",
            ) else 1

            r2 = _post_assignment(
                client,
                interview_id,
                LABEL_RESPONDENT_2,
                "respondent",
                participant_id,
                "__smoke_respondent_2__",
            )
            j2 = r2.get_json(silent=True) or {}
            a2 = j2.get("assignment") or {}
            failures += 0 if print_result(
                "second respondent saves participant_id",
                r2.status_code == 200 and j2.get("ok") is True and a2.get("participant_id") == participant_id,
                f"status={r2.status_code}, participant_id={a2.get('participant_id')}",
            ) else 1

            rm = _post_assignment(
                client,
                interview_id,
                LABEL_MODERATOR,
                "moderator",
                participant_id,
                "__smoke_moderator__",
            )
            jm = rm.get_json(silent=True) or {}
            am = jm.get("assignment") or {}
            failures += 0 if print_result(
                "moderator clears participant_id",
                rm.status_code == 200 and jm.get("ok") is True and am.get("participant_id") is None,
                f"status={rm.status_code}, participant_id={am.get('participant_id')}",
            ) else 1

            ro = _post_assignment(
                client,
                interview_id,
                LABEL_OBSERVER,
                "observer",
                participant_id,
                "__smoke_observer__",
            )
            jo = ro.get_json(silent=True) or {}
            ao = jo.get("assignment") or {}
            failures += 0 if print_result(
                "observer clears participant_id",
                ro.status_code == 200 and jo.get("ok") is True and ao.get("participant_id") is None,
                f"status={ro.status_code}, participant_id={ao.get('participant_id')}",
            ) else 1

            dup = _post_assignment(
                client,
                interview_id,
                LABEL_RESPONDENT_1,
                "respondent",
                participant_id,
                "__smoke_respondent_1_duplicate__",
            )
            dup_json = dup.get_json(silent=True) or {}
            with app.app_context():
                dup_count = SpeakerAssignment.query.filter_by(
                    interview_id=interview_id,
                    speaker_label=LABEL_RESPONDENT_1,
                ).count()
                total_assignment_count = SpeakerAssignment.query.filter_by(
                    interview_id=interview_id,
                ).count()
            failures += 0 if print_result(
                "upsert duplicate prevented",
                dup.status_code == 200
                and dup_json.get("ok") is True
                and dup_json.get("created") is False
                and dup_count == 1,
                f"status={dup.status_code}, created={dup_json.get('created')}, count={dup_count}",
            ) else 1
            failures += 0 if print_result(
                "speaker assignments created",
                total_assignment_count == len(TARGET_LABELS),
                f"count={total_assignment_count}",
            ) else 1

            with app.app_context():
                final_segment_text = {
                    seg_id: db.session.get(Segment, seg_id).text
                    for seg_id in baseline_segment_text
                }
                db.session.remove()
                db.engine.dispose()
            failures += 0 if print_result(
                "segment text unchanged",
                final_segment_text == baseline_segment_text,
            ) else 1
        finally:
            config.DATABASE_URI = original_database_uri
            config.UPLOAD_DIR = original_upload_dir
            config.OUTPUT_DIR = original_output_dir

    repo_path_state_after = _repo_path_state(repo_root)
    failures += 0 if print_result(
        "repo instance/uploads/outputs state unchanged",
        repo_path_state_after == repo_path_state_before,
        f"before={repo_path_state_before}, after={repo_path_state_after}",
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
