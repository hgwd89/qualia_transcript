import subprocess
import sys
import tempfile
from pathlib import Path


FLAG_TYPES = ("favorite", "quote", "exclude", "needs_review")
SEGMENT_TEXT = "Segment flag smoke text"


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


def _repo_path_clean(repo_root: Path, name: str) -> bool:
    return not (repo_root / name).exists()


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    try:
        import config
        from app import create_app
        from models import db
        from models.interview import Interview
        from models.project import Project
        from models.segment import Segment
        from models.segment_flag import SegmentFlag
    except Exception as e:
        print_result("import app/models", False, f"{type(e).__name__}: {e}")
        return 1

    original_database_uri = config.DATABASE_URI
    original_upload_dir = config.UPLOAD_DIR
    original_output_dir = config.OUTPUT_DIR

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        config.DATABASE_URI = f"sqlite:///{(tmp_root / 'flags_smoke.db').as_posix()}"
        config.UPLOAD_DIR = str(tmp_root / "uploads")
        config.OUTPUT_DIR = str(tmp_root / "outputs")
        try:
            app = create_app()
            client = app.test_client()

            failures += 0 if print_result(
                "temporary database configured",
                config.DATABASE_URI.startswith("sqlite:///") and config.DATABASE_URI.endswith("/flags_smoke.db"),
                config.DATABASE_URI,
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_root) in config.UPLOAD_DIR and str(tmp_root) in config.OUTPUT_DIR,
            ) else 1

            with app.app_context():
                project = Project(name="Segment Flag Smoke")
                db.session.add(project)
                db.session.flush()

                interview = Interview(project_id=project.id, status="mapped")
                db.session.add(interview)
                db.session.flush()

                segment = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    start_sec=1.0,
                    end_sec=2.0,
                    text=SEGMENT_TEXT,
                    seq=1,
                )
                db.session.add(segment)
                db.session.commit()

                target_segment_id = segment.id
                baseline_text = segment.text
                baseline_count = SegmentFlag.query.filter_by(segment_id=target_segment_id).count()

            failures += 0 if print_result(
                "temporary segment created",
                target_segment_id is not None and baseline_count == 0,
                f"segment_id={target_segment_id}",
            ) else 1

            quote_note = "__smoke_quote__"
            r1 = client.post(
                f"/api/segments/{target_segment_id}/flags",
                json={"flag_type": "quote", "note": quote_note},
            )
            j1 = r1.get_json(silent=True) or {}
            failures += 0 if print_result(
                "create quote flag",
                r1.status_code == 200 and j1.get("ok") is True,
                f"status={r1.status_code}, created={j1.get('created')}",
            ) else 1

            with app.app_context():
                before_dup = SegmentFlag.query.filter_by(
                    segment_id=target_segment_id,
                    flag_type="quote",
                ).count()
            r2 = client.post(
                f"/api/segments/{target_segment_id}/flags",
                json={"flag_type": "quote", "note": quote_note},
            )
            j2 = r2.get_json(silent=True) or {}
            with app.app_context():
                after_dup = SegmentFlag.query.filter_by(
                    segment_id=target_segment_id,
                    flag_type="quote",
                ).count()
            failures += 0 if print_result(
                "duplicate quote prevented",
                r2.status_code == 200 and j2.get("ok") is True and before_dup == after_dup == 1,
                f"status={r2.status_code}, created={j2.get('created')}, count={after_dup}",
            ) else 1

            for ft in ("favorite", "exclude", "needs_review"):
                r = client.post(
                    f"/api/segments/{target_segment_id}/flags",
                    json={"flag_type": ft, "note": f"__smoke_{ft}__"},
                )
                j = r.get_json(silent=True) or {}
                failures += 0 if print_result(
                    f"create {ft} flag",
                    r.status_code == 200 and j.get("ok") is True,
                    f"status={r.status_code}, created={j.get('created')}",
                ) else 1

            with app.app_context():
                current_flags = {
                    f.flag_type
                    for f in SegmentFlag.query.filter_by(segment_id=target_segment_id).all()
                }
            failures += 0 if print_result(
                "all flag types created",
                current_flags == set(FLAG_TYPES),
                f"flags={sorted(current_flags)}",
            ) else 1

            for ft in FLAG_TYPES:
                rd = client.delete(f"/api/segments/{target_segment_id}/flags/{ft}")
                jd = rd.get_json(silent=True) or {}
                failures += 0 if print_result(
                    f"delete {ft} flag",
                    rd.status_code == 200 and jd.get("ok") is True,
                    f"status={rd.status_code}, deleted={jd.get('deleted')}",
                ) else 1

            with app.app_context():
                final_count = SegmentFlag.query.filter_by(segment_id=target_segment_id).count()
                final_text = Segment.query.get(target_segment_id).text
                db.session.remove()
                db.engine.dispose()

            failures += 0 if print_result(
                "flags are separate from Segment.text",
                final_count == 0,
                f"final_flag_count={final_count}",
            ) else 1
            failures += 0 if print_result(
                "segment text unchanged",
                final_text == baseline_text == SEGMENT_TEXT,
            ) else 1
        finally:
            config.DATABASE_URI = original_database_uri
            config.UPLOAD_DIR = original_upload_dir
            config.OUTPUT_DIR = original_output_dir

    for path_name in ("instance", "uploads", "outputs"):
        failures += 0 if print_result(
            f"repo {path_name}/ not created",
            _repo_path_clean(repo_root, path_name),
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
