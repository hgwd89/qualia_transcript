import json
import sys
import tempfile
from pathlib import Path


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")
    return 0 if ok else 1


def main():
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    failures = 0
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_pipeline_resilience_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'pipeline.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.processing_jobs import execute_job, retry_failed_job
            import services.project_pipeline as pipeline_service

            app = create_app()
            client = app.test_client()
            with app.app_context():
                project = Project(name="Pipeline resilience smoke")
                db.session.add(project)
                db.session.flush()
                iv1 = Interview(project_id=project.id, status="transcribed")
                iv2 = Interview(project_id=project.id, status="transcribed")
                iv3 = Interview(project_id=project.id, status="pending")
                db.session.add_all([iv1, iv2, iv3])
                db.session.flush()
                job = ProcessingJob(
                    project_id=project.id,
                    job_type="project_pipeline",
                    status="pending",
                    worker_pid=4242,
                )
                db.session.add(job)
                db.session.commit()
                project_id, iv1_id, iv2_id, iv3_id, job_id = project.id, iv1.id, iv2.id, iv3.id, job.id

            originals = (
                pipeline_service.auto_assign_speaker_roles,
                pipeline_service.run_mapping,
                pipeline_service.analyze_interview_summary,
            )
            fail_first = {"enabled": True}
            processed = []

            def fake_roles(interview_id):
                processed.append((interview_id, "roles"))

            def fake_mapping(interview_id):
                processed.append((interview_id, "map"))
                if interview_id == iv1_id and fail_first["enabled"]:
                    raise RuntimeError("intentional mapping failure")
                interview = db.session.get(Interview, interview_id)
                interview.status = "mapped"
                db.session.commit()
                return 1

            class Analysis:
                def __init__(self, ident):
                    self.id = ident

            def fake_analysis(interview_id):
                processed.append((interview_id, "analyze"))
                interview = db.session.get(Interview, interview_id)
                interview.status = "analyzed"
                db.session.commit()
                return Analysis(1000 + interview_id)

            pipeline_service.auto_assign_speaker_roles = fake_roles
            pipeline_service.run_mapping = fake_mapping
            pipeline_service.analyze_interview_summary = fake_analysis
            try:
                with app.app_context():
                    failed_job = execute_job(job_id)
                    payload = json.loads(failed_job.result_json or "{}")
                    rows = {row["interview_id"]: row for row in payload.get("interviews") or []}
                    failures += check("partial pipeline is failed", failed_job.status == "failed")
                    failures += check("failed terminal job clears worker pid", failed_job.worker_pid is None)
                    failures += check(
                        "partial result is persisted",
                        payload.get("interview_count") == 3
                        and payload.get("failed_interview_count") == 2
                        and payload.get("missing_media_count") == 1
                        and len(rows) == 3,
                        str(payload),
                    )
                    failures += check(
                        "failed interview keeps step error",
                        any(step.get("error") for step in rows.get(iv1_id, {}).get("steps", [])),
                    )
                    failures += check(
                        "missing media is not silently successful",
                        any(step.get("code") == "missing_media" for step in rows.get(iv3_id, {}).get("steps", [])),
                        str(rows.get(iv3_id)),
                    )
                    failures += check(
                        "later processable interview still completes",
                        db.session.get(Interview, iv2_id).status == "analyzed"
                        and (iv2_id, "map") in processed
                        and (iv2_id, "analyze") in processed,
                        str(processed),
                    )
                    failures += check(
                        "partial failure progress is explicit",
                        (failed_job.to_dict().get("progress") or {}).get("stage") == "failed_partial",
                    )

                    # Simulate the operator resolving the missing-media interview
                    # before retrying the same durable project job.
                    db.session.get(Interview, iv3_id).status = "analyzed"
                    db.session.commit()
                    retry_failed_job(failed_job)
                    failed_job.worker_pid = 4343
                    db.session.commit()
                    fail_first["enabled"] = False
                    retried = execute_job(job_id)
                    retry_payload = json.loads(retried.result_json or "{}")
                    failures += check(
                        "retry resumes remaining work and succeeds after blockers are fixed",
                        retried.status == "succeeded"
                        and db.session.get(Interview, iv1_id).status == "analyzed"
                        and db.session.get(Interview, iv2_id).status == "analyzed"
                        and db.session.get(Interview, iv3_id).status == "analyzed"
                        and retry_payload.get("failed_interview_count") == 0,
                        str(retry_payload),
                    )
                    failures += check("successful terminal job clears worker pid", retried.worker_pid is None)
                    failures += check(
                        "completed interview is not remapped on retry",
                        processed.count((iv2_id, "map")) == 1,
                        str(processed),
                    )

                    missing_job = ProcessingJob(
                        project_id=project_id,
                        job_type="map",
                        status="pending",
                        worker_pid=4444,
                    )
                    db.session.add(missing_job)
                    db.session.commit()
                    missing = execute_job(missing_job.id, handlers={"other": lambda _: {}})
                    failures += check(
                        "missing handler becomes failed",
                        missing.status == "failed"
                        and "no handler" in (missing.error_message or "")
                        and missing.worker_pid is None,
                    )

                status_response = client.get(f"/api/projects/{project_id}/processing-status")
                status_data = status_response.get_json() or {}
                failures += check(
                    "project status API exposes latest pipeline job",
                    status_response.status_code == 200
                    and status_data.get("ok") is True
                    and status_data.get("latest_job", {}).get("id") == job_id
                    and status_data.get("latest_job", {}).get("status") == "succeeded",
                    str(status_data),
                )

                page_response = client.get(f"/projects/{project_id}")
                failures += check(
                    "project page contains durable pipeline polling UI",
                    page_response.status_code == 200
                    and b"pollProjectJob" in page_response.data
                    and b"processing-status" in page_response.data
                    and b"job_id" in page_response.data,
                    f"status={page_response.status_code}",
                )
            finally:
                (
                    pipeline_service.auto_assign_speaker_roles,
                    pipeline_service.run_mapping,
                    pipeline_service.analyze_interview_summary,
                ) = originals
        except Exception as exc:
            failures += check("project pipeline resilience smoke", False, f"{type(exc).__name__}: {exc}")
        finally:
            if app is not None:
                try:
                    from models import db
                    with app.app_context():
                        db.session.remove()
                        db.engine.dispose()
                except Exception:
                    pass
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    print("\nSummary: " + ("PASS" if failures == 0 else f"FAIL ({failures} checks failed)"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
