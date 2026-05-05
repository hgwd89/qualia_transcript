import subprocess
import sys
from pathlib import Path


TARGET_INTERVIEW_ID = 10
TARGET_SPEAKER_LABEL = "C01_C"


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
    baseline_text = None

    try:
        from app import create_app
        from models import db
        from models.interview import Interview
        from models.participant import Participant
        from models.segment import Segment
        from models.speaker_assignment import SpeakerAssignment
    except Exception as e:
        print_result("import app/models", False, f"{type(e).__name__}: {e}")
        return 1

    app = create_app()
    client = app.test_client()

    with app.app_context():
        interview = Interview.query.get(TARGET_INTERVIEW_ID)
        if not interview:
            print_result("target interview exists", False, f"interview_id={TARGET_INTERVIEW_ID} not found")
            return 1

        # label決定: 既定ラベルを優先、なければ先頭distinct
        target_label = TARGET_SPEAKER_LABEL
        seg = Segment.query.filter_by(interview_id=interview.id, speaker_label=target_label).order_by(Segment.id.asc()).first()
        if not seg:
            d = (
                Segment.query
                .with_entities(Segment.speaker_label)
                .filter(Segment.interview_id == interview.id, Segment.speaker_label.isnot(None))
                .distinct()
                .first()
            )
            if not d or not d[0]:
                print_result("speaker_label exists", False, "no speaker_label in interview")
                return 1
            target_label = d[0]
            seg = Segment.query.filter_by(interview_id=interview.id, speaker_label=target_label).order_by(Segment.id.asc()).first()

        baseline_text = seg.text
        baseline_assignment = SpeakerAssignment.query.filter_by(
            interview_id=interview.id, speaker_label=target_label
        ).first()
        baseline = None if not baseline_assignment else {
            "speaker_role": baseline_assignment.speaker_role,
            "participant_id": baseline_assignment.participant_id,
            "note": baseline_assignment.note,
        }

        p01 = Participant.query.filter_by(project_id=interview.project_id, participant_code="P01").first()
        fallback = Participant.query.filter_by(project_id=interview.project_id).order_by(Participant.id.asc()).first()
        participant = p01 or fallback
        if not participant:
            print_result("participant exists", False, f"project_id={interview.project_id} has no participants")
            return 1

    # page check
    r_page = client.get(f"/interviews/{TARGET_INTERVIEW_ID}/speakers")
    failures += 0 if print_result(
        "GET /interviews/<id>/speakers",
        r_page.status_code == 200,
        f"status={r_page.status_code}",
    ) else 1

    # upsert 1
    r1 = client.post(
        f"/api/interviews/{TARGET_INTERVIEW_ID}/speakers/{target_label}",
        json={
            "speaker_role": "respondent",
            "participant_id": participant.id,
            "note": "__smoke_speaker_assign_1__",
        },
    )
    j1 = r1.get_json(silent=True) or {}
    failures += 0 if print_result(
        "upsert assignment #1",
        r1.status_code == 200 and j1.get("ok") is True,
        f"status={r1.status_code}, created={j1.get('created')}",
    ) else 1

    # upsert 2 (duplicate prevention)
    r2 = client.post(
        f"/api/interviews/{TARGET_INTERVIEW_ID}/speakers/{target_label}",
        json={
            "speaker_role": "respondent",
            "participant_id": participant.id,
            "note": "__smoke_speaker_assign_2__",
        },
    )
    j2 = r2.get_json(silent=True) or {}
    with app.app_context():
        cnt = SpeakerAssignment.query.filter_by(
            interview_id=TARGET_INTERVIEW_ID, speaker_label=target_label
        ).count()
    failures += 0 if print_result(
        "upsert duplicate prevented",
        r2.status_code == 200 and j2.get("ok") is True and cnt == 1,
        f"status={r2.status_code}, created={j2.get('created')}, count={cnt}",
    ) else 1

    # cleanup restore baseline
    with app.app_context():
        cur = SpeakerAssignment.query.filter_by(
            interview_id=TARGET_INTERVIEW_ID, speaker_label=target_label
        ).first()
        cleanup_ok = False
        if baseline is None:
            if cur:
                db.session.delete(cur)
                db.session.commit()
            cleanup_ok = (
                SpeakerAssignment.query.filter_by(
                    interview_id=TARGET_INTERVIEW_ID, speaker_label=target_label
                ).count() == 0
            )
        else:
            if cur is None:
                cur = SpeakerAssignment(interview_id=TARGET_INTERVIEW_ID, speaker_label=target_label)
                db.session.add(cur)
            cur.speaker_role = baseline["speaker_role"]
            cur.participant_id = baseline["participant_id"]
            cur.note = baseline["note"]
            db.session.commit()
            restored = SpeakerAssignment.query.filter_by(
                interview_id=TARGET_INTERVIEW_ID, speaker_label=target_label
            ).first()
            cleanup_ok = bool(
                restored
                and restored.speaker_role == baseline["speaker_role"]
                and restored.participant_id == baseline["participant_id"]
                and restored.note == baseline["note"]
            )
        final_text = Segment.query.get(seg.id).text

    failures += 0 if print_result("cleanup restored baseline", cleanup_ok) else 1
    failures += 0 if print_result("segment text unchanged", final_text == baseline_text) else 1

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
