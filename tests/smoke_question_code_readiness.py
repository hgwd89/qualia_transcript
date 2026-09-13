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
        "BACKUP_DIR": config.BACKUP_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_question_readiness_") as tmp:
        root = Path(tmp)
        db_path = root / "question-readiness.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.UPLOAD_DIR = str(root / "uploads")
        config.BACKUP_DIR = str(backup_dir)

        try:
            import app as app_module
            from app import create_app
            from models import db
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.project import Project
            from scripts.audit_production_readiness import audit

            flask_app = create_app()
            flask_app.config["TESTING"] = True

            with flask_app.app_context():
                project = Project(name="Question readiness")
                db.session.add(project)
                db.session.flush()

                flow1 = InterviewFlow(project_id=project.id, title="Flow v1")
                flow2 = InterviewFlow(project_id=project.id, title="Flow v2")
                db.session.add_all([flow1, flow2])
                db.session.flush()

                sec1 = InterviewFlowSection(flow_id=flow1.id, title="S1", seq=1)
                sec1b = InterviewFlowSection(flow_id=flow1.id, title="S2", seq=2)
                sec2 = InterviewFlowSection(flow_id=flow2.id, title="S1", seq=1)
                db.session.add_all([sec1, sec1b, sec2])
                db.session.flush()

                db.session.add_all([
                    InterviewFlowQuestion(
                        section_id=sec1.id,
                        question_code="Q1",
                        question_text="first flow question",
                        seq=1,
                    ),
                    InterviewFlowQuestion(
                        section_id=sec2.id,
                        question_code="Q1",
                        question_text="same code in another flow is valid",
                        seq=1,
                    ),
                ])
                db.session.commit()

                # Simulate a legacy database that already contained a duplicate
                # before forward-only startup guards existed.
                db.session.execute(db.text("DROP TRIGGER IF EXISTS trg_question_code_insert"))
                db.session.execute(db.text("DROP TRIGGER IF EXISTS trg_question_code_update"))
                db.session.commit()
                db.session.add(
                    InterviewFlowQuestion(
                        section_id=sec1b.id,
                        question_code="Q1",
                        question_text="historical duplicate",
                        seq=1,
                    )
                )
                db.session.commit()
                app_module._install_question_code_guards()

                flow1_id = flow1.id
                db.session.remove()
                db.engine.dispose()

            report = audit(db_path, output_dir, backup_dir)
            duplicate_blockers = [
                item
                for item in report.get("blockers", [])
                if item.get("code") == "duplicate_question_code"
            ]
            rows = (
                duplicate_blockers[0].get("context", {}).get("rows", [])
                if duplicate_blockers
                else []
            )
            failures += check(
                "readiness blocks historical same-flow duplicate question codes",
                len(duplicate_blockers) == 1
                and len(rows) == 1
                and int(rows[0].get("flow_id")) == int(flow1_id)
                and rows[0].get("question_code") == "Q1"
                and int(rows[0].get("duplicate_count")) == 2,
                f"blockers={duplicate_blockers}",
            )
            failures += check(
                "same code in another flow is not treated as a duplicate",
                len(rows) == 1,
                f"rows={rows}",
            )
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
