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
    original_uri = config.DATABASE_URI

    with tempfile.TemporaryDirectory(prefix="qualia_fk_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'fk.db').as_posix()}"
        try:
            from sqlalchemy import text
            from sqlalchemy.exc import IntegrityError
            from app import create_app
            from models import db
            from models.project import Project
            from models.participant import Participant

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                pragma_value = db.session.execute(text("PRAGMA foreign_keys")).scalar()
                failures += check(
                    "SQLite foreign key pragma is enabled",
                    int(pragma_value or 0) == 1,
                    f"foreign_keys={pragma_value}",
                )

                project = Project(name="Valid parent")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                valid = Participant(
                    project_id=project_id,
                    participant_code="P01",
                )
                db.session.add(valid)
                db.session.commit()
                failures += check(
                    "valid foreign key insert succeeds",
                    valid.id is not None and valid.project_id == project_id,
                )

                rejected = False
                db.session.add(Participant(
                    project_id=project_id + 999999,
                    participant_code="BAD",
                ))
                try:
                    db.session.commit()
                except IntegrityError:
                    rejected = True
                    db.session.rollback()
                failures += check(
                    "invalid foreign key insert is rejected",
                    rejected,
                )

                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original_uri

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
