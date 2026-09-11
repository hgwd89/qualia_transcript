from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    text = (root / "templates" / "interviews" / "detail.html").read_text(encoding="utf-8")
    failures = 0

    failures += check(
        "interview UI polls durable processing job status",
        "async function pollInterviewJob(jobId, jobType)" in text
        and "/api/processing-jobs/${jobId}" in text
        and "job.status === 'succeeded'" in text
        and "job.status === 'failed'" in text,
    )
    failures += check(
        "interview UI resumes active durable jobs after reload",
        "async function resumeActiveInterviewJob()" in text
        and "/api/interviews/${interviewId}/status" in text
        and "document.addEventListener('DOMContentLoaded', resumeActiveInterviewJob);" in text,
    )
    failures += check(
        "transcribe map analyze use shared durable action",
        "runDurableInterviewAction('transcribe'" in text
        and "runDurableInterviewAction('map'" in text
        and "runDurableInterviewAction('analyze'" in text,
    )
    failures += check(
        "queued response is not treated as immediate completion",
        "${r.segment_count}" not in text
        and "${r.mapped_count}" not in text
        and "if(queued.queued)" in text
        and "queued.job_id" in text,
    )

    if failures:
        print(f"[FAIL] interview durable job UI smoke: {failures} failure(s)")
        return 1
    print("[PASS] interview durable job UI smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
