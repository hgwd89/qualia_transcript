import subprocess
import sys
from pathlib import Path

from docx import Document
from openpyxl import load_workbook


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


def is_ignored(path: str, repo_root: Path) -> bool:
    proc = run_git(repo_root, "check-ignore", "-q", path)
    return proc.returncode == 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    cleanup_failures = 0

    try:
        import config
        from app import create_app
        from models import db
        from models.segment import Segment
        from models.segment_flag import SegmentFlag
        from services.report_verbatim import generate_verbatim
        from services.report_formatted import generate_formatted_sheet
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = create_app()
    with app.app_context():
        seg = Segment.query.get(TARGET_SEGMENT_ID)
        if not seg:
            print_result("target segment exists", False, f"segment_id={TARGET_SEGMENT_ID} not found")
            return 1

        baseline_text = seg.text
        baseline_flags = {
            f.flag_type: (f.note or "")
            for f in SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID).all()
        }
        failures += 0 if print_result(
            "record baseline",
            True,
            f"segment_id={TARGET_SEGMENT_ID}, baseline_flags={sorted(list(baseline_flags.keys()))}",
        ) else 1

        # Add all target flags temporarily (preserve baseline notes if already present).
        created_flags = []
        for flag_type in FLAG_TYPES:
            row = SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID, flag_type=flag_type).first()
            if row is None:
                row = SegmentFlag(
                    segment_id=TARGET_SEGMENT_ID,
                    flag_type=flag_type,
                    note=f"__smoke_outputs_flags_{flag_type}__",
                )
                db.session.add(row)
                db.session.flush()
                created_flags.append(row.id)
        db.session.commit()
        failures += 0 if print_result(
            "temporary flags attached",
            True,
            f"created_count={len(created_flags)}",
        ) else 1

        gf_word = None
        gf_excel = None
        word_path = None
        excel_path = None

        try:
            gf_word = generate_verbatim(seg.interview_id)
            word_path = Path(config.OUTPUT_DIR) / gf_word.stored_path
            failures += 0 if print_result(
                "generate_verbatim",
                True,
                f"file_id={gf_word.id}, filename={gf_word.original_filename}",
            ) else 1
        except Exception as e:
            failures += 0 if print_result("generate_verbatim", False, f"{type(e).__name__}: {e}") else 1

        try:
            gf_excel = generate_formatted_sheet(seg.interview.project_id)
            excel_path = Path(config.OUTPUT_DIR) / gf_excel.stored_path
            failures += 0 if print_result(
                "generate_formatted_sheet",
                True,
                f"file_id={gf_excel.id}, filename={gf_excel.original_filename}",
            ) else 1
        except Exception as e:
            failures += 0 if print_result("generate_formatted_sheet", False, f"{type(e).__name__}: {e}") else 1

        # Word: marker check
        if word_path and word_path.is_file():
            doc = Document(str(word_path))
            texts = [p.text for p in doc.paragraphs]
            marker_found = any("★引用候補" in t for t in texts)
            marker_with_target = any(("★引用候補" in t and baseline_text in t) for t in texts)
            failures += 0 if print_result(
                "word marker present",
                marker_found,
                f"path={word_path.name}",
            ) else 1
            failures += 0 if print_result(
                "word marker linked to target segment",
                marker_with_target,
            ) else 1
        else:
            failures += 0 if print_result("word output exists", False, str(word_path)) else 1

        # Excel: columns + values
        if excel_path and excel_path.is_file():
            wb = load_workbook(str(excel_path), data_only=True)
            ws_unclassified = wb["未分類発言"]

            headers = [str(c.value) if c.value is not None else "" for c in ws_unclassified[1]]
            header_ok = all(name in headers for name in FLAG_TYPES)
            failures += 0 if print_result(
                "excel unclassified flag columns",
                header_ok,
                ",".join(headers),
            ) else 1

            flag_values_ok = False
            if header_ok and "発言テキスト" in headers:
                text_col = headers.index("発言テキスト") + 1
                idx = {name: headers.index(name) + 1 for name in FLAG_TYPES}
                for r in range(2, ws_unclassified.max_row + 1):
                    if ws_unclassified.cell(r, text_col).value == baseline_text:
                        vals = {
                            k: str(ws_unclassified.cell(r, c).value or "").strip().lower()
                            for k, c in idx.items()
                        }
                        if all(vals[k] == "true" for k in FLAG_TYPES):
                            flag_values_ok = True
                            break

            failures += 0 if print_result(
                "excel flag values true for target segment",
                flag_values_ok,
            ) else 1
        else:
            failures += 0 if print_result("excel output exists", False, str(excel_path)) else 1

        # outputs ignore check
        ignored_ok = True
        if gf_word is not None:
            ignored_ok = ignored_ok and is_ignored(
                str(Path("outputs") / gf_word.stored_path.replace("\\", "/")),
                repo_root,
            )
        if gf_excel is not None:
            ignored_ok = ignored_ok and is_ignored(
                str(Path("outputs") / gf_excel.stored_path.replace("\\", "/")),
                repo_root,
            )
        failures += 0 if print_result("generated outputs are git-ignored", ignored_ok) else 1

        # Cleanup flags: return to baseline
        for flag_type in FLAG_TYPES:
            row = SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID, flag_type=flag_type).first()
            if flag_type not in baseline_flags:
                if row is not None:
                    db.session.delete(row)
            else:
                if row is None:
                    row = SegmentFlag(
                        segment_id=TARGET_SEGMENT_ID,
                        flag_type=flag_type,
                        note=baseline_flags[flag_type],
                    )
                    db.session.add(row)
                else:
                    row.note = baseline_flags[flag_type]
        db.session.commit()

        final_flags = {
            f.flag_type: (f.note or "")
            for f in SegmentFlag.query.filter_by(segment_id=TARGET_SEGMENT_ID).all()
        }
        if final_flags != baseline_flags:
            cleanup_failures += 1
        failures += 0 if print_result(
            "cleanup restored baseline flags",
            final_flags == baseline_flags and cleanup_failures == 0,
            f"final={sorted(list(final_flags.keys()))}, baseline={sorted(list(baseline_flags.keys()))}",
        ) else 1

        final_text = Segment.query.get(TARGET_SEGMENT_ID).text
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
