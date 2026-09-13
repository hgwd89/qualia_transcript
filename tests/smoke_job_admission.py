import ast
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def _uses_legacy_admission(source_text: str) -> bool:
    """Detect real imports/calls without treating comments or strings as call sites."""
    tree = ast.parse(source_text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "services.processing_jobs":
            if any(alias.name == "create_or_get_active_job" for alias in node.names):
                return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "create_or_get_active_job":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "create_or_get_active_job":
                return True
    return False


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    legacy_callers = []
    parse_errors = []
    for folder_name in ("routes", "services", "scripts"):
        folder = repo_root / folder_name
        for source_path in folder.rglob("*.py"):
            if source_path == repo_root / "services" / "processing_jobs.py":
                continue
            try:
                source_text = source_path.read_text(encoding="utf-8")
                if _uses_legacy_admission(source_text):
                    legacy_callers.append(source_path.relative_to(repo_root).as_posix())
            except (OSError, SyntaxError) as exc:
                parse_errors.append(
                    f"{source_path.relative_to(repo_root).as_posix()}: {type(exc).__name__}: {exc}"
                )
    failures += check(
        "operational Python sources are parseable for admission-boundary audit",
        not parse_errors,
        "; ".join(parse_errors),
    )
    failures += check(
        "operational code does not bypass authoritative job admission",
        not legacy_callers,
        ", ".join(legacy_callers),
    )

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_job_admission_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'admission.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_admission import admit_processing_job, admit_retry_job

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Atomic admission smoke")
                other_project = Project(name="Other project")
                db.session.add_all([project, other_project])
                db.session.flush()

                flow = InterviewFlow(project_id=project.id, title="Primary flow")
                alternate_flow = InterviewFlow(project_id=project.id, title="Alternate flow")
                other_flow = InterviewFlow(project_id=other_project.id, title="Other flow")
                db.session.add_all([flow, alternate_flow, other_flow])
                db.session.flush()

                section = InterviewFlowSection(flow_id=flow.id, title="Primary", seq=1)
                alternate_section = InterviewFlowSection(flow_id=alternate_flow.id, title="Alternate", seq=1)
                other_section = InterviewFlowSection(flow_id=other_flow.id, title="Other", seq=1)
                db.session.add_all([section, alternate_section, other_section])
                db.session.flush()

                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Primary question",
                    seq=1,
                )
                alternate_question = InterviewFlowQuestion(
                    section_id=alternate_section.id,
                    question_code="Q2",
                    question_text="Alternate question",
                    seq=1,
                )
                other_question = InterviewFlowQuestion(
                    section_id=other_section.id,
                    question_code="QX",
                    question_text="Other project question",
                    seq=1,
                )
                db.session.add_all([question, alternate_question, other_question])
                db.session.flush()

                iv1 = Interview(project_id=project.id, flow_id=flow.id, status="transcribed")
                iv2 = Interview(project_id=project.id, flow_id=flow.id, status="transcribed")
                other_iv = Interview(project_id=other_project.id, flow_id=other_flow.id, status="transcribed")
                db.session.add_all([iv1, iv2, other_iv])
                db.session.commit()

                project_id = int(project.id)
                iv1_id = int(iv1.id)
                iv2_id = int(iv2.id)
                other_iv_id = int(other_iv.id)
                question_id = int(question.id)
                alternate_question_id = int(alternate_question.id)
                other_question_id = int(other_question.id)

            def concurrent_admit(specs):
                barrier = threading.Barrier(len(specs))

                def worker(spec):
                    job_type, interview_id = spec
                    with app.app_context():
                        barrier.wait(timeout=10)
                        admission = admit_processing_job(project_id, job_type, interview_id)
                        return {
                            "job_id": admission.job_id,
                            "created": admission.created,
                            "conflict_job_id": admission.conflict_job_id,
                            "error": admission.error,
                        }

                with ThreadPoolExecutor(max_workers=len(specs)) as pool:
                    return list(pool.map(worker, specs))

            def finish_active_jobs():
                with app.app_context():
                    for job in ProcessingJob.query.filter_by(project_id=project_id).filter(
                        ProcessingJob.status.in_(("pending", "running"))
                    ).all():
                        job.status = "succeeded"
                        job.worker_pid = None
                        job.finished_at = datetime.now(timezone.utc)
                    db.session.commit()

            with app.app_context():
                valid_question = admit_processing_job(
                    project_id,
                    "analyze_question",
                    iv1_id,
                    question_id=question_id,
                )
                failures += check(
                    "valid interview/question scope is admitted",
                    valid_question.created and valid_question.job_id is not None and valid_question.error is None,
                    str(valid_question),
                )
            finish_active_jobs()

            with app.app_context():
                before_count = ProcessingJob.query.count()
                wrong_project_interview = admit_processing_job(project_id, "map", other_iv_id)
                wrong_project_question = admit_processing_job(
                    project_id,
                    "analyze_cross",
                    question_id=other_question_id,
                )
                wrong_interview_flow = admit_processing_job(
                    project_id,
                    "analyze_question",
                    iv1_id,
                    question_id=alternate_question_id,
                )
                malformed_specs = [
                    admit_processing_job(project_id, "transcribe", None),
                    admit_processing_job(project_id, "project_pipeline", iv1_id),
                    admit_processing_job(project_id, "analyze_integrated", question_id=question_id),
                    admit_processing_job(project_id, "analyze_cross", None),
                ]
                after_count = ProcessingJob.query.count()
                failures += check(
                    "service boundary rejects interview from another project",
                    wrong_project_interview.error is not None and wrong_project_interview.job_id is None,
                    str(wrong_project_interview),
                )
                failures += check(
                    "service boundary rejects question from another project",
                    wrong_project_question.error is not None and wrong_project_question.job_id is None,
                    str(wrong_project_question),
                )
                failures += check(
                    "question analysis rejects question from another flow in same project",
                    wrong_interview_flow.error is not None and wrong_interview_flow.job_id is None,
                    str(wrong_interview_flow),
                )
                failures += check(
                    "job-type required/forbidden scope fields are enforced",
                    all(item.error is not None and item.job_id is None for item in malformed_specs),
                    str(malformed_specs),
                )
                failures += check(
                    "rejected scope combinations write no durable rows",
                    before_count == after_count,
                    f"before={before_count} after={after_count}",
                )

                corrupt_retry = ProcessingJob(
                    project_id=project_id,
                    interview_id=other_iv_id,
                    job_type="analyze",
                    status="failed",
                    error_message="legacy cross-project scope",
                )
                malformed_retry = ProcessingJob(
                    project_id=project_id,
                    interview_id=None,
                    job_type="map",
                    status="failed",
                    error_message="legacy malformed scope",
                )
                db.session.add_all([corrupt_retry, malformed_retry])
                db.session.commit()
                corrupt_retry_id = int(corrupt_retry.id)
                malformed_retry_id = int(malformed_retry.id)

                corrupt_result = admit_retry_job(corrupt_retry_id)
                malformed_result = admit_retry_job(malformed_retry_id)
                corrupt_after = db.session.get(ProcessingJob, corrupt_retry_id)
                malformed_after = db.session.get(ProcessingJob, malformed_retry_id)
                failures += check(
                    "retry refuses legacy cross-project durable job",
                    corrupt_result.error is not None
                    and corrupt_after.status == "failed"
                    and corrupt_after.error_message == "legacy cross-project scope",
                    f"result={corrupt_result} row={corrupt_after.to_dict()}",
                )
                failures += check(
                    "retry refuses legacy malformed durable job scope",
                    malformed_result.error is not None
                    and malformed_after.status == "failed"
                    and malformed_after.error_message == "legacy malformed scope",
                    f"result={malformed_result} row={malformed_after.to_dict()}",
                )

            same = concurrent_admit([("map", iv1_id), ("map", iv1_id)])
            same_ids = {row["job_id"] for row in same if row["job_id"] is not None}
            failures += check(
                "same-scope concurrent admission deduplicates to one job",
                len(same_ids) == 1
                and sum(1 for row in same if row["created"]) == 1
                and sum(1 for row in same if not row["created"] and row["conflict_job_id"] is None and row["error"] is None) == 1,
                str(same),
            )
            with app.app_context():
                active = ProcessingJob.query.filter_by(project_id=project_id, interview_id=iv1_id, status="pending").all()
                failures += check("same-scope race leaves one active row", len(active) == 1, str([j.id for j in active]))
            finish_active_jobs()

            interview_conflict = concurrent_admit([("map", iv1_id), ("analyze", iv1_id)])
            created_rows = [row for row in interview_conflict if row["created"]]
            conflict_rows = [row for row in interview_conflict if row["conflict_job_id"]]
            failures += check(
                "different job types on one interview serialize admission",
                len(created_rows) == 1
                and len(conflict_rows) == 1
                and conflict_rows[0]["conflict_job_id"] == created_rows[0]["job_id"],
                str(interview_conflict),
            )
            with app.app_context():
                active = ProcessingJob.query.filter_by(project_id=project_id, interview_id=iv1_id, status="pending").all()
                failures += check("interview conflict race leaves one active row", len(active) == 1, str([j.id for j in active]))
            finish_active_jobs()

            project_conflict = concurrent_admit([("project_pipeline", None), ("map", iv2_id)])
            created_rows = [row for row in project_conflict if row["created"]]
            conflict_rows = [row for row in project_conflict if row["conflict_job_id"]]
            failures += check(
                "project pipeline and interview job serialize admission",
                len(created_rows) == 1
                and len(conflict_rows) == 1
                and conflict_rows[0]["conflict_job_id"] == created_rows[0]["job_id"],
                str(project_conflict),
            )
            with app.app_context():
                active = ProcessingJob.query.filter_by(project_id=project_id, status="pending").all()
                failures += check("project conflict race leaves one active row", len(active) == 1, str([j.id for j in active]))
            finish_active_jobs()

            with app.app_context():
                failed_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=iv1_id,
                    job_type="analyze",
                    status="failed",
                    error_message="old failure",
                )
                db.session.add(failed_job)
                db.session.commit()
                failed_job_id = failed_job.id

            retry_barrier = threading.Barrier(2)

            def retry_worker(_):
                with app.app_context():
                    retry_barrier.wait(timeout=10)
                    admission = admit_retry_job(failed_job_id)
                    return {
                        "job_id": admission.job_id,
                        "created": admission.created,
                        "conflict_job_id": admission.conflict_job_id,
                        "error": admission.error,
                    }

            with ThreadPoolExecutor(max_workers=2) as pool:
                retry_rows = list(pool.map(retry_worker, range(2)))
            failures += check(
                "concurrent retry requeues failed job once",
                sum(1 for row in retry_rows if row["created"]) == 1
                and sum(1 for row in retry_rows if row["error"] == "only failed jobs can be retried") == 1,
                str(retry_rows),
            )
            with app.app_context():
                retried = db.session.get(ProcessingJob, failed_job_id)
                failures += check(
                    "retry race leaves target pending exactly once",
                    retried.status == "pending" and retried.error_message is None,
                    str(retried.to_dict()),
                )
                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check("job admission smoke", False, f"{type(exc).__name__}: {exc}")
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
