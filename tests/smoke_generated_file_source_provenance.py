import json
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def has_code(items: list[dict], code: str, *, file_id: int | None = None) -> bool:
    for item in items:
        if item.get("code") != code:
            continue
        if file_id is None:
            return True
        context = item.get("context") or {}
        if int(context.get("generated_file_id") or 0) == int(file_id):
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
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }

    with tempfile.TemporaryDirectory(prefix="qualia_output_source_provenance_") as tmp:
        root = Path(tmp)
        db_path = root / "source.db"
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
            from audit_production_readiness_final import audit_final
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.participant import Participant, ParticipantAttribute
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from models.segment_flag import SegmentFlag
            from models.speaker_assignment import SpeakerAssignment
            from services.file_manager import (
                generated_file_source_provenance,
                open_output_target_for_write,
                prepare_output_target,
                register_generated_file,
            )
            from services.generated_file_source_provenance import (
                GeneratedFileSourceProvenanceError,
                capture_generated_file_source_provenance,
            )
            from services.generated_file_source_provenance_sqlite import (
                capture_generated_file_source_provenance_sqlite,
            )
            from services.report_analysis import generate_analysis_csv
            from services.report_formatted import generate_formatted_sheet
            from services.report_verbatim import generate_verbatim

            app = create_app()
            app.config["TESTING"] = True
            with app.app_context():
                project = Project(name="Source provenance", client="Client")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                participant = Participant(
                    project_id=project_id,
                    participant_code="P01",
                    display_name="Participant One",
                )
                db.session.add(participant)
                db.session.commit()
                participant_id = int(participant.id)
                db.session.add(ParticipantAttribute(
                    participant_id=participant_id,
                    attribute_key="年代",
                    attribute_value="30代",
                    display_order=1,
                ))

                flow = InterviewFlow(project_id=project_id, title="Main Flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(
                    flow_id=int(flow.id),
                    title="Section A",
                    seq=1,
                )
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=int(section.id),
                    question_code="Q1",
                    question_text="How was it?",
                    is_key_question=True,
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()

                interview = Interview(
                    project_id=project_id,
                    participant_id=participant_id,
                    flow_id=int(flow.id),
                    interview_date=date(2026, 1, 2),
                    interviewer_name="Moderator",
                )
                db.session.add(interview)
                db.session.flush()
                interview_id = int(interview.id)

                segment = Segment(
                    interview_id=interview_id,
                    participant_id=participant_id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    start_sec=1.0,
                    end_sec=2.0,
                    text="Original source statement",
                    seq=1,
                )
                db.session.add(segment)
                db.session.flush()
                segment_id = int(segment.id)
                db.session.add(UtteranceMapping(
                    segment_id=segment_id,
                    question_id=int(question.id),
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                ))
                db.session.add(SpeakerAssignment(
                    interview_id=interview_id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    participant_id=participant_id,
                ))
                db.session.add(SegmentFlag(segment_id=segment_id, flag_type="quote"))
                db.session.commit()

                orm_provenance = {
                    file_type: capture_generated_file_source_provenance(
                        file_type,
                        project_id,
                        interview_id=interview_id if file_type == "verbatim" else None,
                    )
                    for file_type in ("verbatim", "formatted_sheet", "analysis")
                }

                con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    sqlite_provenance = {
                        file_type: capture_generated_file_source_provenance_sqlite(
                            con,
                            file_type,
                            project_id,
                            interview_id=interview_id if file_type == "verbatim" else None,
                        )
                        for file_type in ("verbatim", "formatted_sheet", "analysis")
                    }
                finally:
                    con.close()
                failures += check(
                    "ORM and readiness SQLite source fingerprints are identical for all ordinary deliverables",
                    orm_provenance == sqlite_provenance,
                    f"orm={orm_provenance} sqlite={sqlite_provenance}",
                )

                verbatim = generate_verbatim(interview_id)
                formatted = generate_formatted_sheet(project_id)
                analysis = generate_analysis_csv(project_id)
                generated = [verbatim, formatted, analysis]
                generated_ids = [int(row.id) for row in generated]
                failures += check(
                    "ordinary report generators persist generation-time source provenance",
                    all(generated_file_source_provenance(row) is not None for row in generated),
                )

                client = app.test_client()
                initial_statuses = [
                    client.get(f"/api/outputs/{file_id}/download").status_code
                    for file_id in generated_ids
                ]
                failures += check(
                    "current source-bound ordinary deliverables are downloadable",
                    initial_statuses == [200, 200, 200],
                    str(initial_statuses),
                )

                initial_readiness = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                )
                source_info = initial_readiness.get("info", {}).get(
                    "generated_file_source_provenance", {}
                )
                failures += check(
                    "final readiness proves all generated ordinary deliverables current",
                    source_info.get("current") == 3
                    and source_info.get("invalid") == 0,
                    f"info={source_info} blockers={initial_readiness.get('blockers', [])}",
                )

                # Capture a valid analysis snapshot, generate bytes, then mutate
                # the canonical source before registration.  The registrar must
                # reject the stale bytes while holding its write reservation.
                race_provenance = capture_generated_file_source_provenance(
                    "analysis",
                    project_id,
                )
                race_target = prepare_output_target(project_id, "race.csv")
                race_opened = open_output_target_for_write(race_target)
                try:
                    race_opened.stream.write(b"a,b\r\n1,2\r\n")
                finally:
                    race_opened.close()
                race_path = Path(race_target.full_path)

                segment = db.session.get(Segment, segment_id)
                segment.text = "Changed before registration"
                db.session.commit()
                rejected = False
                try:
                    register_generated_file(
                        race_target,
                        project_id=project_id,
                        file_type="analysis",
                        file_format="csv",
                        source_provenance=race_provenance,
                    )
                except GeneratedFileSourceProvenanceError:
                    rejected = True
                failures += check(
                    "registration rejects source drift after bytes were generated and removes stale bytes",
                    rejected and not race_path.exists(),
                    f"rejected={rejected} path_exists={race_path.exists()}",
                )

                stale_statuses = [
                    client.get(f"/api/outputs/{file_id}/download").status_code
                    for file_id in generated_ids
                ]
                failures += check(
                    "ordinary downloads fail closed after canonical source mutation",
                    stale_statuses == [409, 409, 409],
                    str(stale_statuses),
                )

                stale_readiness = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                )
                stale_info = stale_readiness.get("info", {}).get(
                    "generated_file_source_provenance", {}
                )
                failures += check(
                    "final readiness blocks stale ordinary deliverables",
                    stale_info.get("invalid") == 3
                    and has_code(
                        stale_readiness.get("blockers", []),
                        "generated_file_source_provenance_invalid",
                    ),
                    f"info={stale_info} blockers={stale_readiness.get('blockers', [])}",
                )

                # A legacy row without source provenance remains readable for
                # backward compatibility but is explicitly unproven in readiness.
                legacy_target = prepare_output_target(project_id, "legacy.csv")
                legacy_opened = open_output_target_for_write(legacy_target)
                try:
                    legacy_opened.stream.write(b"legacy,value\r\n1,2\r\n")
                finally:
                    legacy_opened.close()
                legacy = register_generated_file(
                    legacy_target,
                    project_id=project_id,
                    file_type="analysis",
                    file_format="csv",
                )
                legacy_id = int(legacy.id)
                failures += check(
                    "legacy provenance-free ordinary artifact remains downloadable",
                    client.get(f"/api/outputs/{legacy_id}/download").status_code == 200,
                )
                legacy_readiness = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                )
                failures += check(
                    "legacy provenance-free ordinary artifact is warning-only and explicit",
                    has_code(
                        legacy_readiness.get("warnings", []),
                        "generated_file_source_provenance_unproven",
                        file_id=legacy_id,
                    ),
                    str(legacy_readiness.get("warnings", [])),
                )

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
