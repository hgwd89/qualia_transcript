import subprocess
import sys
from pathlib import Path


TARGET_INTERVIEW_ID = 10
TARGET_LABELS = ("C01_A", "C02_A", "C01_C", "C02_B")
MODERATOR_LABELS = ("C01_A", "C02_A")
RESPONDENT_LABELS = ("C01_C", "C02_B")


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
    baseline_assignments = {}
    baseline_segment_text = {}

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

        p01 = Participant.query.filter_by(project_id=interview.project_id, participant_code="P01").first()
        if not p01:
            print_result("P01 exists", False, f"project_id={interview.project_id} has no P01")
            return 1

        missing_labels = []
        for label in TARGET_LABELS:
            seg = (
                Segment.query
                .filter_by(interview_id=interview.id, speaker_label=label)
                .order_by(Segment.id.asc())
                .first()
            )
            if not seg:
                missing_labels.append(label)
                continue

            baseline_segment_text[seg.id] = seg.text
            assignment = SpeakerAssignment.query.filter_by(
                interview_id=interview.id,
                speaker_label=label,
            ).first()
            baseline_assignments[label] = None if assignment is None else {
                "speaker_role": assignment.speaker_role,
                "participant_id": assignment.participant_id,
                "note": assignment.note,
            }

        failures += 0 if print_result(
            "target labels exist",
            len(missing_labels) == 0,
            f"missing={missing_labels}" if missing_labels else "all labels found",
        ) else 1
        if missing_labels:
            return 1

    page = client.get(f"/interviews/{TARGET_INTERVIEW_ID}/speakers")
    failures += 0 if print_result(
        "GET /interviews/<id>/speakers",
        page.status_code == 200,
        f"status={page.status_code}",
    ) else 1

    def post_assignment(label: str, role: str, participant_id, note: str):
        return client.post(
            f"/api/interviews/{TARGET_INTERVIEW_ID}/speakers/{label}",
            json={
                "speaker_role": role,
                "participant_id": participant_id,
                "note": note,
            },
        )

    for label in MODERATOR_LABELS:
        r = post_assignment(label, "moderator", p01.id, f"__smoke_{label}_moderator__")
        j = r.get_json(silent=True) or {}
        ok = r.status_code == 200 and j.get("ok") is True and (j.get("assignment") or {}).get("participant_id") is None
        failures += 0 if print_result(
            f"{label} save moderator with blank participant",
            ok,
            f"status={r.status_code}, participant_id={(j.get('assignment') or {}).get('participant_id')}",
        ) else 1

    for label in RESPONDENT_LABELS:
        r = post_assignment(label, "respondent", p01.id, f"__smoke_{label}_respondent__")
        j = r.get_json(silent=True) or {}
        ok = r.status_code == 200 and j.get("ok") is True and (j.get("assignment") or {}).get("participant_id") == p01.id
        failures += 0 if print_result(
            f"{label} save respondent with P01",
            ok,
            f"status={r.status_code}, participant_id={(j.get('assignment') or {}).get('participant_id')}",
        ) else 1

    dup = post_assignment("C01_C", "respondent", p01.id, "__smoke_C01_C_duplicate__")
    dup_json = dup.get_json(silent=True) or {}
    with app.app_context():
        dup_count = SpeakerAssignment.query.filter_by(
            interview_id=TARGET_INTERVIEW_ID,
            speaker_label="C01_C",
        ).count()
    failures += 0 if print_result(
        "upsert duplicate prevented for C01_C",
        dup.status_code == 200 and dup_json.get("ok") is True and dup_count == 1,
        f"status={dup.status_code}, created={dup_json.get('created')}, count={dup_count}",
    ) else 1

    with app.app_context():
        cleanup_ok = True
        for label in TARGET_LABELS:
            cur = SpeakerAssignment.query.filter_by(
                interview_id=TARGET_INTERVIEW_ID,
                speaker_label=label,
            ).first()
            base = baseline_assignments.get(label)

            if base is None:
                if cur is not None:
                    db.session.delete(cur)
            else:
                if cur is None:
                    cur = SpeakerAssignment(interview_id=TARGET_INTERVIEW_ID, speaker_label=label)
                    db.session.add(cur)
                cur.speaker_role = base["speaker_role"]
                cur.participant_id = base["participant_id"]
                cur.note = base["note"]

        db.session.commit()

        for label in TARGET_LABELS:
            cur = SpeakerAssignment.query.filter_by(
                interview_id=TARGET_INTERVIEW_ID,
                speaker_label=label,
            ).first()
            base = baseline_assignments.get(label)
            if base is None and cur is not None:
                cleanup_ok = False
            elif base is not None:
                if not cur:
                    cleanup_ok = False
                elif (
                    cur.speaker_role != base["speaker_role"]
                    or cur.participant_id != base["participant_id"]
                    or cur.note != base["note"]
                ):
                    cleanup_ok = False

        text_ok = True
        for seg_id, text in baseline_segment_text.items():
            seg = Segment.query.get(seg_id)
            if not seg or seg.text != text:
                text_ok = False
                break

    failures += 0 if print_result("cleanup restored baseline", cleanup_ok) else 1
    failures += 0 if print_result("segment text unchanged", text_ok) else 1

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
