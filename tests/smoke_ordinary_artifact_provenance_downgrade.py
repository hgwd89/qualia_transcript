from __future__ import annotations

import json
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
    root_dir = Path(__file__).resolve().parents[1]
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_ordinary_provenance_downgrade_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'downgrade.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        try:
            from app import create_app
            from models import db
            from models.generated_file import GeneratedFile
            from models.interview import Interview
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment
            from services.ordinary_artifact_provenance import (
                ORDINARY_PROVENANCE_KEY,
                ordinary_artifact_currentness,
            )
            from services.report_analysis import generate_analysis_csv

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Downgrade guard")
                db.session.add(project)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant",
                )
                db.session.add(participant)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                db.session.add(Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="source statement",
                    seq=1,
                ))
                db.session.commit()

                generated = generate_analysis_csv(int(project.id))
                file_id = int(generated.id)
                params = json.loads(generated.generation_params_json or "{}")
                failures += check(
                    "generated analysis starts with declared provenance",
                    isinstance(params.get(ORDINARY_PROVENANCE_KEY), dict),
                    f"params={params}",
                )

                params[ORDINARY_PROVENANCE_KEY] = "corrupted-provenance"
                generated.generation_params_json = json.dumps(
                    params,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                db.session.commit()
                db.session.expire_all()

                generated = db.session.get(GeneratedFile, file_id)
                status = ordinary_artifact_currentness(generated)
                failures += check(
                    "malformed declared provenance cannot downgrade to legacy mode",
                    status.provenance_present and not status.current,
                    status.reason,
                )

                response = client.get(f"/api/outputs/{file_id}/download")
                failures += check(
                    "malformed declared provenance is rejected at download",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "ordinary provenance downgrade smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
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

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
