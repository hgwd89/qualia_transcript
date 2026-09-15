from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def has_mapping_blocker(report: dict, interview_id: int) -> bool:
    for item in report.get("blockers", []):
        if item.get("code") != "mapping_input_currentness_invalid":
            continue
        context = item.get("context") or {}
        if int(context.get("interview_id") or 0) == int(interview_id):
            return True
    return False


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    for entry in (repo_root, scripts_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_mapping_readiness_") as tmp:
        root = Path(tmp)
        db_path = root / "mapping-readiness.db"
        upload_dir = root / "uploads"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        upload_dir.mkdir()
        output_dir.mkdir()
        backup_dir.mkdir()
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(upload_dir)
        config.OUTPUT_DIR = str(output_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            from audit_production_readiness_final import audit_final
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.project import Project
            from models.segment import Segment, UtteranceMapping, UtteranceMappingProvenance
            from services.mapping_source_provenance import (
                build_mapping_source_manifest,
                mapping_source_provenance_for_manifest,
                serialize_mapping_source_provenance,
            )

            app = create_app()
            app.config["TESTING"] = True
            with app.app_context():
                project = Project(name="Mapping readiness")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=0)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="質問",
                    seq=0,
                )
                db.session.add(question)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="生成時回答",
                    seq=0,
                )
                db.session.add(segment)
                db.session.flush()

                proof = serialize_mapping_source_provenance(
                    mapping_source_provenance_for_manifest(
                        build_mapping_source_manifest(interview.id)
                    )
                )
                mapping = UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="ai",
                    confidence=0.95,
                    is_unclassified=False,
                )
                db.session.add(mapping)
                db.session.flush()
                db.session.add(UtteranceMappingProvenance(
                    mapping_id=mapping.id,
                    source_provenance_json=proof,
                ))
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                segment_id = int(segment.id)

                current_global = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                )
                current_project = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                    project_id=project_id,
                )
                failures += check(
                    "final readiness accepts current proven AI mapping input",
                    not has_mapping_blocker(current_global, interview_id)
                    and not has_mapping_blocker(current_project, interview_id),
                    f"global={current_global.get('blockers', [])} project={current_project.get('blockers', [])}",
                )

                segment = db.session.get(Segment, segment_id)
                segment.text = "完了後に変更された回答"
                db.session.commit()
                db.session.remove()

                stale_global = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                )
                stale_project = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                    project_id=project_id,
                )
                failures += check(
                    "database-wide final readiness blocks stale AI mapping input",
                    has_mapping_blocker(stale_global, interview_id),
                    str(stale_global.get("blockers", [])),
                )
                failures += check(
                    "project-scoped final readiness blocks stale AI mapping input",
                    has_mapping_blocker(stale_project, interview_id),
                    str(stale_project.get("blockers", [])),
                )
                mapping_info = stale_project.get("info", {}).get(
                    "mapping_input_currentness",
                    {},
                )
                failures += check(
                    "final readiness exposes mapping currentness counts",
                    int(mapping_info.get("checked") or 0) == 1
                    and int(mapping_info.get("invalid") or 0) == 1,
                    str(mapping_info),
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "mapping input readiness smoke",
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
            if original["BACKUP_DIR"] is not None:
                config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
