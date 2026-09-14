import ast
import sys
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    source_path = repo_root / "tests" / "smoke_analysis.py"
    source = source_path.read_text(encoding="utf-8")

    try:
        tree = ast.parse(source)
        failures += check("provider smoke source parses", True)
    except SyntaxError as exc:
        failures += check("provider smoke source parses", False, str(exc))
        return 1

    failures += check(
        "real database credential lookup is read-only",
        "?mode=ro" in source and 'PRAGMA query_only=ON' in source,
    )
    failures += check(
        "analysis smoke allocates a temporary workspace",
        "tempfile.TemporaryDirectory" in source,
    )
    failures += check(
        "SQLAlchemy database is redirected to temporary SQLite",
        'config.DATABASE_URI = f"sqlite:///{temp_db.as_posix()}"' in source,
    )
    failures += check(
        "managed runtime paths are redirected to temporary workspace",
        all(
            marker in source
            for marker in (
                'config.UPLOAD_DIR = str(root / "uploads")',
                'config.OUTPUT_DIR = str(root / "outputs")',
                'config.BACKUP_DIR = str(root / "backups")',
                'config.RUNTIME_LOCK_PATH = str(root / "runtime.lock")',
            )
        ),
    )
    failures += check(
        "analysis fixture creates its own project participant interview and segment",
        all(
            marker in source
            for marker in (
                "Project(name=\"Provider smoke fixture\"",
                "Participant(",
                "Interview(",
                "Segment(",
            )
        ),
    )

    guarded_analysis_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "analyze_interview_summary"
        ):
            continue
        keyword_names = {kw.arg for kw in node.keywords if kw.arg}
        guarded_analysis_calls.append("result_write_guard" in keyword_names)

    failures += check(
        "provider smoke analysis write is explicitly guarded",
        bool(guarded_analysis_calls) and all(guarded_analysis_calls),
        f"calls={guarded_analysis_calls}",
    )

    forbidden_live_patterns = [
        "Interview.query.get(4)",
        "Interview.query.get(interview_id)",
        "interview_id = 4",
    ]
    failures += check(
        "provider smoke does not select a pre-existing research interview",
        not any(pattern in source for pattern in forbidden_live_patterns),
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
