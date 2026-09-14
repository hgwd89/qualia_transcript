import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def has_code(items: list[dict], code: str) -> bool:
    return any(item.get("code") == code for item in items)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    for entry in (repo_root, scripts_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }
    failures = 0

    with tempfile.TemporaryDirectory(prefix="qualia_final_readiness_watch_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        output_dir = root / "outputs"
        upload_dir = root / "uploads"
        backup_dir = root / "backups"
        output_dir.mkdir()
        upload_dir.mkdir()
        backup_dir.mkdir()
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.UPLOAD_DIR = str(upload_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            from models import db
            from models.project import Project
            import audit_production_readiness_final as final_audit

            app = create_app()
            with app.app_context():
                project = Project(name="Before final audit")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)
                db.session.remove()
                db.engine.dispose()

            original_v2 = final_audit.readiness_v2.audit

            def fake_v2(*args, **kwargs):
                con = sqlite3.connect(db_path)
                try:
                    con.execute(
                        "UPDATE projects SET name=? WHERE id=?",
                        ("Changed during final audit", project_id),
                    )
                    con.commit()
                finally:
                    con.close()
                return {"blockers": [], "warnings": [], "info": {}}

            final_audit.readiness_v2.audit = fake_v2
            try:
                changed = final_audit.audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                )
            finally:
                final_audit.readiness_v2.audit = original_v2

            failures += check(
                "composite readiness blocks a commit between v2 and ownership checks",
                has_code(
                    changed.get("blockers", []),
                    "database_changed_during_final_ownership_audit",
                ),
                str(changed),
            )

            original_v2 = final_audit.readiness_v2.audit
            final_audit.readiness_v2.audit = lambda *args, **kwargs: {
                "blockers": [], "warnings": [], "info": {}
            }
            try:
                stable = final_audit.audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                )
            finally:
                final_audit.readiness_v2.audit = original_v2

            failures += check(
                "composite readiness does not invent database-change blockers on a stable DB",
                not has_code(
                    stable.get("blockers", []),
                    "database_changed_during_final_ownership_audit",
                )
                and not has_code(
                    stable.get("blockers", []),
                    "final_database_change_detection_failed",
                ),
                str(stable),
            )
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            if original["BACKUP_DIR"] is not None:
                config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
