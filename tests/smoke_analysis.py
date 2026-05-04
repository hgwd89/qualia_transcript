import json
import re
import subprocess
import sys
from pathlib import Path


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def run_git_status(repo_root: Path, label: str) -> bool:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    print(f"--- git status --short ({label}) ---")
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.strip())
    print("--- end ---")
    return proc.returncode == 0


def contains_japanese(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text or ""))


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    failures += 0 if print_result(
        "git status --short before",
        run_git_status(repo_root, "before"),
    ) else 1

    try:
        # OpenAI 到達を阻害する壊れたプロキシを、このプロセス限定で除去
        import os
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
            os.environ.pop(k, None)

        from app import create_app
        from models.interview import Interview
        from models.segment import Segment
        from models.analysis import AIAnalysis
        import services.analyzer as analyzer
    except Exception as e:
        failures += 0 if print_result("imports", False, f"{type(e).__name__}: {e}") else 1
        print("\nSummary: FAIL")
        return 1

    app = create_app()

    with app.app_context():
        interview_id = 4
        interview = Interview.query.get(interview_id)
        failures += 0 if print_result("interview_id=4 exists", interview is not None) else 1
        if interview is None:
            print("\nSummary: FAIL")
            return 1

        respondent_segments = Segment.query.filter_by(
            interview_id=interview_id, speaker_role="respondent"
        ).count()
        failures += 0 if print_result(
            "respondent segment exists", respondent_segments > 0, f"count={respondent_segments}"
        ) else 1

        status_before = interview.status
        count_before = AIAnalysis.query.count()

        counter = {"n": 0}
        original_call = analyzer.call_structured

        def wrapped_call(*args, **kwargs):
            counter["n"] += 1
            return original_call(*args, **kwargs)

        analyzer.call_structured = wrapped_call

        analyze_ok = True
        error_detail = ""
        saved_id = None
        try:
            analysis = analyzer.analyze_interview_summary(interview_id)
            saved_id = analysis.id
        except Exception as e:
            analyze_ok = False
            error_detail = f"{type(e).__name__}: {e}"
        finally:
            analyzer.call_structured = original_call

        failures += 0 if print_result(
            "analyze_interview_summary(4) executed once",
            analyze_ok,
            error_detail,
        ) else 1
        failures += 0 if print_result(
            "OpenAI API call count == 1",
            counter["n"] == 1,
            f"count={counter['n']}",
        ) else 1

        count_after = AIAnalysis.query.count()
        failures += 0 if print_result(
            "AIAnalysis count increased",
            count_after == count_before + 1,
            f"before={count_before}, after={count_after}",
        ) else 1

        interview_after = Interview.query.get(interview_id)
        status_after = interview_after.status if interview_after else None
        failures += 0 if print_result(
            "Interview.status becomes analyzed",
            status_after == "analyzed",
            f"before={status_before}, after={status_after}",
        ) else 1

        latest = AIAnalysis.query.get(saved_id) if saved_id else None
        failures += 0 if print_result("latest AIAnalysis exists", latest is not None) else 1

        if latest is not None:
            failures += 0 if print_result(
                "analysis_type is per_participant",
                latest.analysis_type == "per_participant",
                f"type={latest.analysis_type}",
            ) else 1

            payload = {}
            try:
                payload = json.loads(latest.content_json or "{}")
            except Exception as e:
                failures += 0 if print_result("content_json parse", False, f"{type(e).__name__}: {e}") else 1

            has_content_json = isinstance(payload, dict) and bool(payload)
            failures += 0 if print_result("content_json exists", has_content_json) else 1

            findings = payload.get("findings", []) if isinstance(payload, dict) else []
            evidence_quotes = []
            if isinstance(findings, list):
                for item in findings:
                    if isinstance(item, dict):
                        q = (item.get("evidence_quote") or "").strip()
                        if q:
                            evidence_quotes.append(q)
            failures += 0 if print_result(
                "evidence_quote exists",
                len(evidence_quotes) > 0,
            ) else 1

            text_for_lang = " ".join(
                [payload.get("implications", ""), payload.get("unresolved", "")] +
                [str(item.get("point", "")) for item in findings if isinstance(item, dict)]
            )
            failures += 0 if print_result(
                "Japanese output (simple check)",
                contains_japanese(text_for_lang),
            ) else 1

    failures += 0 if print_result(
        "git status --short after",
        run_git_status(repo_root, "after"),
    ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
