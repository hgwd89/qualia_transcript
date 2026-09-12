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
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    base = (repo_root / "templates" / "base.html").read_text(encoding="utf-8")
    analysis = (repo_root / "templates" / "analysis" / "index.html").read_text(encoding="utf-8")
    shared = (repo_root / "static" / "js" / "processing_jobs.js").read_text(encoding="utf-8")

    failures += check(
        "shared processing-job script loads after page content",
        "{% block content %}{% endblock %}" in base
        and "static', filename='js/processing_jobs.js'" in base
        and base.index("{% block content %}{% endblock %}")
            < base.index("static', filename='js/processing_jobs.js'"),
    )
    failures += check(
        "analysis page exposes poll and button-busy globals",
        "function pollAnalysisJob(jobId, btn, runningLabel)" in analysis
        and "function setAnalysisButtonBusy(btn, busy)" in analysis
        and "resumeActiveAnalysisJob" in analysis,
    )
    wrapper_pos = shared.find("const originalPollAnalysisJob = window.pollAnalysisJob")
    interview_pos = shared.find("const interviewId = interviewIdFromPath()")
    failures += check(
        "shared script wraps project-analysis polling before interview-only return",
        wrapper_pos >= 0
        and interview_pos >= 0
        and wrapper_pos < interview_pos
        and "window.pollAnalysisJob = async (jobId, btn, runningLabel)" in shared,
    )
    failures += check(
        "analysis poll failure restores the associated button before rethrow",
        "window.setAnalysisButtonBusy(btn, false);" in shared
        and "throw error;" in shared,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
