import sys
import tempfile
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

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_generated_file_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'generated.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")

        try:
            from sqlalchemy import event

            from app import create_app
            from models import db
            from models.generated_file import GeneratedFile
            from models.project import Project
            from services.file_manager import (
                file_exists,
                get_full_path,
                prepare_output_target,
                register_generated_file,
                safe_output_filename,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Path / unsafe : project ?")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                target = prepare_output_target(
                    project_id,
                    "分析/../レポート:危険?.xlsx",
                )
                target_path = Path(target.full_path)
                output_root = Path(config.OUTPUT_DIR).resolve()
                project_dir = (output_root / str(project_id)).resolve()
                failures += check(
                    "output target remains project-scoped",
                    target_path.parent == project_dir
                    and target_path.name == target.filename
                    and target.stored_path == f"{project_id}/{target.filename}",
                    f"target={target}",
                )
                failures += check(
                    "generated filename is cross-platform safe",
                    all(ch not in target.filename for ch in '<>:"/\\|?*')
                    and "\\" not in target.stored_path,
                    target.filename,
                )

                target_path.write_bytes(b"generated")
                gf = register_generated_file(
                    target,
                    project_id=project_id,
                    file_type="analysis",
                    file_format="xlsx",
                )
                failures += check(
                    "successful registration keeps file and DB row aligned",
                    gf.id is not None
                    and file_exists(gf)
                    and Path(get_full_path(gf)) == target_path,
                    f"file_id={gf.id} stored_path={gf.stored_path}",
                )

                before_count = GeneratedFile.query.count()
                failed_target = prepare_output_target(project_id, "commit_failure.csv")
                failed_path = Path(failed_target.full_path)
                failed_path.write_text("orphan candidate", encoding="utf-8")

                session = db.session()

                def fail_before_commit(_session):
                    raise RuntimeError("simulated generated-file DB commit failure")

                event.listen(session, "before_commit", fail_before_commit, once=True)
                raised = False
                try:
                    register_generated_file(
                        failed_target,
                        project_id=project_id,
                        file_type="analysis",
                        file_format="csv",
                    )
                except RuntimeError as exc:
                    raised = "simulated generated-file DB commit failure" in str(exc)
                finally:
                    if event.contains(session, "before_commit", fail_before_commit):
                        event.remove(session, "before_commit", fail_before_commit)

                failures += check(
                    "DB registration failure removes generated orphan",
                    raised
                    and not failed_path.exists()
                    and GeneratedFile.query.count() == before_count,
                    f"raised={raised} exists={failed_path.exists()} count={GeneratedFile.query.count()}",
                )

                outside = root / "outside.txt"
                outside.write_text("must survive", encoding="utf-8")
                malicious = GeneratedFile(
                    project_id=project_id,
                    file_type="analysis",
                    file_format="csv",
                    original_filename="outside.txt",
                    stored_path="../outside.txt",
                )
                db.session.add(malicious)
                db.session.commit()
                invalid_rejected = False
                try:
                    get_full_path(malicious)
                except ValueError:
                    invalid_rejected = True
                failures += check(
                    "malicious stored_path cannot escape OUTPUT_DIR",
                    invalid_rejected and not file_exists(malicious) and outside.is_file(),
                )

                client = app.test_client()
                response = client.get(f"/api/outputs/{malicious.id}/download")
                failures += check(
                    "download route rejects escaped stored_path",
                    response.status_code == 404 and outside.is_file(),
                    f"status={response.status_code}",
                )

                invalid_name_raised = False
                try:
                    safe_output_filename("...")
                except ValueError:
                    invalid_name_raised = True
                failures += check(
                    "empty-after-sanitize filename is rejected",
                    invalid_name_raised,
                )
                failures += check(
                    "Windows reserved filename is neutralized",
                    safe_output_filename("CON.txt") == "_CON.txt",
                )

                # Windows keeps SQLite files locked while pooled connections are open.
                db.session.remove()
                db.engine.dispose()

            wired = {
                "services/report_verbatim.py": "file_type=\"verbatim\"",
                "services/report_formatted.py": "file_type=\"formatted_sheet\"",
                "services/report_analysis.py": "file_type=\"analysis\"",
                "services/report_approved_analysis.py": "file_type=\"approved_analysis\"",
            }
            for relative, marker in wired.items():
                text = (repo_root / relative).read_text(encoding="utf-8")
                failures += check(
                    f"{relative} uses managed generated-file registration",
                    "prepare_output_target" in text
                    and "register_generated_file" in text
                    and marker in text,
                )

        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
