import json
import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def currentness_blockers(report: dict) -> list[dict]:
    return [
        item for item in report.get("blockers", [])
        if item.get("code") == "approved_analysis_artifact_currentness_invalid"
    ]


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }

    with tempfile.TemporaryDirectory(prefix="qualia_readiness_formal_currentness_") as tmp:
        root = Path(tmp)
        db_path = root / "currentness.db"
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
            from audit_production_readiness_project import audit_project
            from audit_production_readiness_v2 import audit as audit_readiness
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.analysis_review import set_analysis_review_status
            from services.analysis_source_provenance import capture_analysis_source_provenance
            from services.approved_analysis_currentness import (
                approved_analysis_artifact_currentness,
            )
            from services.approved_analysis_currentness_sqlite import (
                validate_approved_analysis_artifact_currentness,
            )
            from services.report_approved_analysis import generate_approved_analysis_xlsx

            app = create_app()
            app.config["TESTING"] = True

            def create_approved_analysis(project_name: str, participant_code: str):
                project = Project(name=project_name)
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title=f"{project_name} flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="重要なことは何ですか？",
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code=participant_code,
                    display_name=participant_code,
                )
                db.session.add(participant)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                source_text = f"{participant_code} は安心感を重視します。"
                segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text=source_text,
                    seq=0,
                )
                db.session.add(segment)
                db.session.flush()
                db.session.add(UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                ))
                db.session.commit()

                provenance = capture_analysis_source_provenance(
                    "per_question",
                    int(project.id),
                    interview_id=int(interview.id),
                    question_id=int(question.id),
                )
                analysis = AIAnalysis(
                    project_id=project.id,
                    interview_id=interview.id,
                    question_id=question.id,
                    analysis_type="per_question",
                    title="Q1 考察",
                    summary_text="安心感が重要",
                    content_json=json.dumps({
                        "findings": [{
                            "point": "安心感が重要",
                            "evidence_quote": source_text,
                            "source_segment_ids": [int(segment.id)],
                            "participant_codes": [participant_code],
                            "question_codes": ["Q1"],
                            "confidence": "high",
                        }],
                        "implications": "安心感を明示する",
                        "unresolved": "",
                        "source_provenance": provenance,
                    }, ensure_ascii=False),
                    model_used="test",
                    review_status="draft",
                )
                db.session.add(analysis)
                db.session.commit()
                analysis, unresolved = set_analysis_review_status(
                    analysis,
                    "approved",
                    "initial review",
                )
                if unresolved:
                    raise AssertionError(f"approval unexpectedly unresolved: {unresolved}")
                return {
                    "project_id": int(project.id),
                    "interview_id": int(interview.id),
                    "question_id": int(question.id),
                    "segment_id": int(segment.id),
                    "participant_code": participant_code,
                    "source_text": source_text,
                    "analysis_id": int(analysis.id),
                }

            def add_second_approved_analysis(ctx: dict) -> int:
                provenance = capture_analysis_source_provenance(
                    "per_question",
                    ctx["project_id"],
                    interview_id=ctx["interview_id"],
                    question_id=ctx["question_id"],
                )
                analysis = AIAnalysis(
                    project_id=ctx["project_id"],
                    interview_id=ctx["interview_id"],
                    question_id=ctx["question_id"],
                    analysis_type="per_question",
                    title="Q1 追加考察",
                    summary_text="追加の考察",
                    content_json=json.dumps({
                        "findings": [{
                            "point": "追加の考察",
                            "evidence_quote": ctx["source_text"],
                            "source_segment_ids": [ctx["segment_id"]],
                            "participant_codes": [ctx["participant_code"]],
                            "question_codes": ["Q1"],
                            "confidence": "high",
                        }],
                        "implications": "追加示唆",
                        "unresolved": "",
                        "source_provenance": provenance,
                    }, ensure_ascii=False),
                    model_used="test",
                    review_status="draft",
                )
                db.session.add(analysis)
                db.session.commit()
                analysis, unresolved = set_analysis_review_status(
                    analysis,
                    "approved",
                    "second review",
                )
                if unresolved:
                    raise AssertionError(f"second approval unexpectedly unresolved: {unresolved}")
                return int(analysis.id)

            with app.app_context():
                primary = create_approved_analysis("Primary project", "P01")
                first_artifact = generate_approved_analysis_xlsx(primary["project_id"])
                first_artifact_id = int(first_artifact.id)

                orm_state = approved_analysis_artifact_currentness(first_artifact)
                failures += check(
                    "delivery currentness accepts freshly generated formal artifact",
                    orm_state.is_current,
                    orm_state.reason,
                )

                sqlite_con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                sqlite_con.row_factory = sqlite3.Row
                try:
                    sqlite_ok, sqlite_reason = validate_approved_analysis_artifact_currentness(
                        sqlite_con,
                        project_id=primary["project_id"],
                        generation_params_json=first_artifact.generation_params_json,
                    )
                finally:
                    sqlite_con.close()
                failures += check(
                    "read-only SQLite currentness mirrors delivery for fresh artifact",
                    sqlite_ok,
                    sqlite_reason,
                )

                report = audit_readiness(db_path, output_dir, backup_dir)
                failures += check(
                    "readiness accepts formal history with a current artifact",
                    not currentness_blockers(report),
                    str(currentness_blockers(report)),
                )

                analysis = db.session.get(AIAnalysis, primary["analysis_id"])
                analysis, unresolved = set_analysis_review_status(
                    analysis,
                    "approved",
                    "review state changed",
                )
                failures += check(
                    "reapproval used for state drift succeeds",
                    not unresolved,
                    str(unresolved),
                )

                client = app.test_client()
                response = client.get(f"/api/outputs/{first_artifact_id}/download")
                failures += check(
                    "delivery rejects formal artifact after review-state drift",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

                report = audit_readiness(db_path, output_dir, backup_dir)
                blockers = currentness_blockers(report)
                failures += check(
                    "readiness blocks when all registered formal artifacts are stale",
                    bool(blockers)
                    and blockers[0].get("context", {}).get("project_id") == primary["project_id"],
                    str(blockers),
                )

                refreshed = generate_approved_analysis_xlsx(primary["project_id"])
                refreshed_id = int(refreshed.id)
                report = audit_readiness(db_path, output_dir, backup_dir)
                failures += check(
                    "regeneration restores readiness while stale formal history is retained",
                    not currentness_blockers(report),
                    str(currentness_blockers(report)),
                )
                failures += check(
                    "stale predecessor remains registered as audit history",
                    first_artifact_id != refreshed_id,
                    f"first={first_artifact_id} refreshed={refreshed_id}",
                )

                add_second_approved_analysis(primary)
                response = client.get(f"/api/outputs/{refreshed_id}/download")
                failures += check(
                    "delivery rejects prior formal artifact when approved set expands",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()
                report = audit_readiness(db_path, output_dir, backup_dir)
                failures += check(
                    "readiness blocks when approved-set drift leaves no current formal artifact",
                    bool(currentness_blockers(report)),
                    str(currentness_blockers(report)),
                )

                final_artifact = generate_approved_analysis_xlsx(primary["project_id"])
                response = client.get(f"/api/outputs/{int(final_artifact.id)}/download")
                failures += check(
                    "delivery accepts regenerated artifact for expanded approved set",
                    response.status_code == 200,
                    f"status={response.status_code}",
                )
                response.close()
                report = audit_readiness(db_path, output_dir, backup_dir)
                failures += check(
                    "readiness accepts expanded set once a current formal artifact exists",
                    not currentness_blockers(report),
                    str(currentness_blockers(report)),
                )

                secondary = create_approved_analysis("Secondary project", "P02")
                secondary_artifact = generate_approved_analysis_xlsx(secondary["project_id"])
                secondary_analysis = db.session.get(AIAnalysis, secondary["analysis_id"])
                secondary_analysis, unresolved = set_analysis_review_status(
                    secondary_analysis,
                    "approved",
                    "secondary stale state",
                )
                if unresolved:
                    raise AssertionError(f"secondary reapproval unexpectedly unresolved: {unresolved}")

                whole_report = audit_readiness(db_path, output_dir, backup_dir)
                whole_blockers = currentness_blockers(whole_report)
                failures += check(
                    "whole-database readiness sees stale formal history in another project",
                    any(
                        item.get("context", {}).get("project_id") == secondary["project_id"]
                        for item in whole_blockers
                    ),
                    str(whole_blockers),
                )

                scoped_report = audit_project(
                    db_path,
                    output_dir,
                    backup_dir,
                    primary["project_id"],
                )
                scoped_blockers = currentness_blockers(scoped_report)
                failures += check(
                    "project-scoped readiness does not import another project's stale formal history",
                    not scoped_blockers,
                    str(scoped_blockers),
                )

                # Keep the secondary artifact referenced so accidental cleanup or
                # query changes cannot make the fixture silently disappear.
                failures += check(
                    "secondary stale formal artifact remains registered",
                    int(secondary_artifact.id) > 0,
                )

                db.session.rollback()
                db.session.remove()
                db.engine.dispose()
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
