import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def _issue_codes(report: dict) -> set[str]:
    return {str(item.get("code")) for item in report.get("blockers", [])}


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    for path in (repo_root, scripts_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_participant_identity_") as tmp:
        root = Path(tmp)
        db_path = root / "participant-identity.db"
        output_dir = root / "outputs"
        upload_dir = root / "uploads"
        backup_dir = root / "backups"

        # Simulate a pre-constraint installation. Historical duplicates are kept
        # deliberately so startup must protect future writes without rewriting the
        # existing research records.
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                """
                CREATE TABLE participants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    participant_code TEXT NOT NULL,
                    display_name TEXT,
                    created_at DATETIME
                )
                """
            )
            con.execute(
                "INSERT INTO participants(project_id, participant_code, display_name) VALUES (1, 'P01', 'legacy-a')"
            )
            con.execute(
                "INSERT INTO participants(project_id, participant_code, display_name) VALUES (1, 'P01', 'legacy-b')"
            )
            con.commit()
        finally:
            con.close()

        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.UPLOAD_DIR = str(upload_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from sqlalchemy import text
            from sqlalchemy.exc import IntegrityError

            from app import create_app
            from models import db
            from models.participant import Participant
            from models.project import Project
            import audit_production_readiness as base_readiness
            import audit_production_readiness_project as project_readiness

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                # Bind the legacy rows to a real project after create_all has filled
                # in the rest of the schema around the pre-existing participants table.
                project1 = Project(id=1, name="Legacy duplicate participant codes")
                project2 = Project(id=2, name="Clean participant codes")
                db.session.add_all([project1, project2])
                db.session.commit()

                constraint_names = {
                    getattr(constraint, "name", None)
                    for constraint in Participant.__table__.constraints
                }
                failures += check(
                    "new-schema model declares project-local participant code uniqueness",
                    "uq_participant_project_code" in constraint_names,
                    f"constraints={sorted(str(v) for v in constraint_names if v)}",
                )

                legacy_rows = Participant.query.filter_by(
                    project_id=1,
                    participant_code="P01",
                ).order_by(Participant.id.asc()).all()
                failures += check(
                    "startup preserves historical duplicate participant rows",
                    len(legacy_rows) == 2
                    and [p.display_name for p in legacy_rows] == ["legacy-a", "legacy-b"],
                    f"count={len(legacy_rows)}",
                )

                trigger_names = {
                    row[0]
                    for row in db.session.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_participants_code_%'"
                        )
                    ).fetchall()
                }
                failures += check(
                    "legacy SQLite participant-code insert/update guards are installed",
                    trigger_names == {
                        "trg_participants_code_insert",
                        "trg_participants_code_update",
                    },
                    f"triggers={sorted(trigger_names)}",
                )

                duplicate_rejected = False
                db.session.add(Participant(
                    project_id=1,
                    participant_code="P01",
                    display_name="must-not-insert",
                ))
                try:
                    db.session.commit()
                except IntegrityError:
                    duplicate_rejected = True
                    db.session.rollback()
                failures += check(
                    "legacy SQLite guard rejects a new duplicate without rewriting predecessors",
                    duplicate_rejected
                    and Participant.query.filter_by(project_id=1, participant_code="P01").count() == 2,
                )

                client = app.test_client()
                for code in ("P01", "P02", "P03"):
                    response = client.post(
                        "/projects/2/participants/new",
                        data={"participant_code": code, "display_name": code},
                        follow_redirects=False,
                    )
                    failures += check(
                        f"route creates clean participant {code}",
                        response.status_code in {302, 303},
                        f"status={response.status_code}",
                    )

                p02 = Participant.query.filter_by(project_id=2, participant_code="P02").one()
                delete_response = client.post(
                    f"/projects/2/participants/{p02.id}/delete",
                    follow_redirects=False,
                )
                auto_response = client.post(
                    "/projects/2/participants/new",
                    data={"participant_code": "", "display_name": "auto-after-delete"},
                    follow_redirects=False,
                )
                codes_after_delete = [
                    row.participant_code
                    for row in Participant.query.filter_by(project_id=2).order_by(Participant.id.asc()).all()
                ]
                failures += check(
                    "auto participant code is monotonic and does not reuse deleted count slot",
                    delete_response.status_code in {302, 303}
                    and auto_response.status_code in {302, 303}
                    and codes_after_delete == ["P01", "P03", "P04"],
                    f"codes={codes_after_delete}",
                )

                before_duplicate_count = Participant.query.filter_by(project_id=2).count()
                duplicate_response = client.post(
                    "/projects/2/participants/new",
                    data={"participant_code": "P03", "display_name": "duplicate"},
                    follow_redirects=False,
                )
                failures += check(
                    "participant create route rejects explicit project-local duplicate",
                    duplicate_response.status_code == 409
                    and Participant.query.filter_by(project_id=2).count() == before_duplicate_count,
                    f"status={duplicate_response.status_code}",
                )

                p04 = Participant.query.filter_by(project_id=2, participant_code="P04").one()
                edit_response = client.post(
                    f"/projects/2/participants/{p04.id}/edit",
                    data={"participant_code": "P03", "display_name": "must-not-change"},
                    follow_redirects=False,
                )
                db.session.expire_all()
                p04_after = db.session.get(Participant, p04.id)
                failures += check(
                    "participant edit route rejects duplicate and preserves original identity",
                    edit_response.status_code == 409
                    and p04_after is not None
                    and p04_after.participant_code == "P04"
                    and p04_after.display_name == "auto-after-delete",
                    (
                        f"status={edit_response.status_code} "
                        f"code={p04_after.participant_code if p04_after else None}"
                    ),
                )

                db.session.remove()
                db.engine.dispose()

            global_report = base_readiness.audit(db_path, output_dir, backup_dir)
            failures += check(
                "global readiness blocks historical duplicate participant codes",
                "duplicate_participant_code" in _issue_codes(global_report),
                f"blockers={sorted(_issue_codes(global_report))}",
            )

            project1_report = project_readiness.audit_project(
                db_path,
                output_dir,
                backup_dir,
                1,
            )
            project2_report = project_readiness.audit_project(
                db_path,
                output_dir,
                backup_dir,
                2,
            )
            failures += check(
                "project readiness reports duplicate only for owning project",
                "duplicate_participant_code" in _issue_codes(project1_report)
                and "duplicate_participant_code" not in _issue_codes(project2_report),
                (
                    f"p1={sorted(_issue_codes(project1_report))} "
                    f"p2={sorted(_issue_codes(project2_report))}"
                ),
            )
            failures += check(
                "project readiness participant count is project-scoped",
                int((project2_report.get("info") or {}).get("table_counts", {}).get("participants", -1)) == 3,
                f"count={(project2_report.get('info') or {}).get('table_counts', {}).get('participants')}",
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
