import hashlib
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_audit(repo_root: Path):
    scripts_dir = repo_root / "scripts"
    for value in (repo_root, scripts_dir):
        if str(value) not in sys.path:
            sys.path.insert(0, str(value))
    path = scripts_dir / "audit_production_readiness_v2.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_v2_fk", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_readiness_fk_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        output_dir.mkdir(parents=True)
        backup_dir.mkdir(parents=True)
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            from models import db
            from models.project import Project

            app = create_app()
            app.config["TESTING"] = True
            with app.app_context():
                project = Project(name="FK readiness fixture")
                db.session.add(project)
                db.session.commit()
                db.session.remove()
                db.engine.dispose()

            # Simulate legacy corruption produced before FK enforcement existed.
            con = sqlite3.connect(db_path)
            try:
                con.execute("PRAGMA foreign_keys=OFF")
                con.execute(
                    "INSERT INTO participants(project_id, participant_code) VALUES (?, ?)",
                    (999999, "ORPHAN"),
                )
                con.commit()
            finally:
                con.close()

            audit_mod = load_audit(repo_root)
            before = file_hash(db_path)
            report = audit_mod.audit(db_path, output_dir, backup_dir)
            after = file_hash(db_path)
            blocker_codes = {item.get("code") for item in report.get("blockers", [])}

            failures += check(
                "readiness audit blocks existing FK violations",
                "sqlite_foreign_key_violations" in blocker_codes,
                str(blocker_codes),
            )
            failures += check(
                "readiness audit reports FK violation count",
                int(report.get("info", {}).get("foreign_key_violation_count", 0)) >= 1,
                str(report.get("info", {})),
            )
            failures += check(
                "readiness FK audit remains read-only",
                before == after,
            )
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
