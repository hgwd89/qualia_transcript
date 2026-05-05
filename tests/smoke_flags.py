import subprocess
import sys
from pathlib import Path


TARGET_SEGMENT_ID = 257
FLAG_TYPES = ("favorite", "quote", "exclude", "needs_review")


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
    cleanup_failures = 0
    baseline_text = None

    try:
        from app import create_app
        from models import db
        from models.segment import Segment
        from models.segment_flag import SegmentFlag
    except Exception as e:
        print_result("import app/models", False, f"{type(e).__name__}: {e}")
        return 1

    app = create_app()
    client = app.test_client()

    with app.app_context():
        seg = Segment.query.get(TARGET_SEGMENT_ID)
        if not seg:
            print_result("target segment exists", False, f"segment_id={TARGET_SEGMENT_ID} not found")
            return 1

        baseline_text = seg.text
        baseline_flags = {
            f.flag_type: f.note
            for f in SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID).all()
        }
        failures += 0 if print_result(
            "record baseline",
            True,
            f"segment_id={TARGET_SEGMENT_ID}, baseline_flags={sorted(list(baseline_flags.keys()))}",
        ) else 1

    # create quote
    quote_note = baseline_flags.get("quote", "__smoke_quote__")
    r1 = client.post(
        f"/api/segments/{TARGET_SEGMENT_ID}/flags",
        json={"flag_type": "quote", "note": quote_note},
    )
    j1 = r1.get_json(silent=True) or {}
    failures += 0 if print_result(
        "create quote flag",
        r1.status_code == 200 and j1.get("ok") is True,
        f"status={r1.status_code}, created={j1.get('created')}",
    ) else 1

    # duplicate quote create should not duplicate rows
    with app.app_context():
        before_dup = SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID, flag_type="quote").count()
    r2 = client.post(
        f"/api/segments/{TARGET_SEGMENT_ID}/flags",
        json={"flag_type": "quote", "note": quote_note},
    )
    j2 = r2.get_json(silent=True) or {}
    with app.app_context():
        after_dup = SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID, flag_type="quote").count()
    failures += 0 if print_result(
        "duplicate quote prevented",
        r2.status_code == 200 and j2.get("ok") is True and after_dup == before_dup == 1,
        f"status={r2.status_code}, created={j2.get('created')}, count={after_dup}",
    ) else 1

    # create other flags
    for ft in ("favorite", "exclude", "needs_review"):
        note = baseline_flags.get(ft, f"__smoke_{ft}__")
        r = client.post(f"/api/segments/{TARGET_SEGMENT_ID}/flags", json={"flag_type": ft, "note": note})
        j = r.get_json(silent=True) or {}
        failures += 0 if print_result(
            f"create {ft} flag",
            r.status_code == 200 and j.get("ok") is True,
            f"status={r.status_code}, created={j.get('created')}",
        ) else 1

    # cleanup: restore baseline flags
    with app.app_context():
        current = {
            f.flag_type: f.note
            for f in SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID).all()
        }

    # remove flags not in baseline
    for ft in FLAG_TYPES:
        if ft in current and ft not in baseline_flags:
            rd = client.delete(f"/api/segments/{TARGET_SEGMENT_ID}/flags/{ft}")
            jd = rd.get_json(silent=True) or {}
            ok = rd.status_code == 200 and jd.get("ok") is True
            cleanup_failures += 0 if ok else 1
            print_result(f"cleanup delete {ft}", ok, f"status={rd.status_code}, deleted={jd.get('deleted')}")

    # restore baseline flags and notes
    for ft, note in baseline_flags.items():
        rr = client.post(f"/api/segments/{TARGET_SEGMENT_ID}/flags", json={"flag_type": ft, "note": note})
        jr = rr.get_json(silent=True) or {}
        ok = rr.status_code == 200 and jr.get("ok") is True
        cleanup_failures += 0 if ok else 1
        print_result(f"cleanup restore {ft}", ok, f"status={rr.status_code}, created={jr.get('created')}")

    with app.app_context():
        from models.segment_flag import SegmentFlag

        final_flags = {
            f.flag_type: f.note
            for f in SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID).all()
        }
        final_text = Segment.query.get(TARGET_SEGMENT_ID).text

    failures += 0 if print_result(
        "cleanup restored baseline",
        final_flags == baseline_flags and cleanup_failures == 0,
        f"final={sorted(list(final_flags.keys()))}, baseline={sorted(list(baseline_flags.keys()))}",
    ) else 1
    failures += 0 if print_result(
        "segment text unchanged",
        final_text == baseline_text,
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
