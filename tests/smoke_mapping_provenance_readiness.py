from __future__ import annotations

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


def blocker_codes(result) -> set[str]:
    return {str(item.get("code")) for item in result.blockers}


def inspect_db(db_path: Path, project_id: int | None = None):
    from services.mapping_readiness_sqlite import inspect_mapping_provenance_readiness

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return inspect_mapping_provenance_readiness(con, project_id=project_id)
    finally:
        con.close()


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    scripts_dir = repo_root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }

    with tempfile.TemporaryDirectory(prefix="qualia_mapping_readiness_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        uploads = root / "uploads"
        outputs = root / "outputs"
        backups = root / "backups"
        uploads.mkdir()
        outputs.mkdir()
        backups.mkdir()

        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(uploads)
        config.OUTPUT_DIR = str(outputs)
        config.BACKUP_DIR = str(backups)

        app = None
        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.project import Project
            from models.segment import (
                Segment,
                UtteranceMapping,
                UtteranceMappingProvenance,
            )
            from services.mapping_source_provenance import (
                capture_mapping_source_provenance,
                serialize_mapping_source_provenance,
            )
            import audit_production_readiness_final as final_readiness

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Mapping readiness project")
                db.session.add(project)
                db.session.flush()

                flow = InterviewFlow(project_id=project.id, title="Guide")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(
                    flow_id=flow.id,
                    title="Section",
                    description="",
                    seq=1,
                )
                db.session.add(section)
                db.session.flush()
                q1 = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="First question",
                    question_type="open",
                    seq=1,
                )
                q2 = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q2",
                    question_text="Second question",
                    question_type="open",
                    seq=2,
                )
                db.session.add_all([q1, q2])
                db.session.flush()

                interview = Interview(
                    project_id=project.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                s1 = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="first answer",
                    seq=1,
                )
                s2 = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="second answer",
                    seq=2,
                )
                db.session.add_all([s1, s2])
                db.session.flush()

                proof = serialize_mapping_source_provenance(
                    capture_mapping_source_provenance(int(interview.id))
                )
                m1 = UtteranceMapping(
                    segment_id=s1.id,
                    question_id=q1.id,
                    mapped_by="ai",
                    confidence=0.95,
                    is_unclassified=False,
                )
                m2 = UtteranceMapping(
                    segment_id=s2.id,
                    question_id=q2.id,
                    mapped_by="ai",
                    confidence=0.95,
                    is_unclassified=False,
                )
                db.session.add_all([m1, m2])
                db.session.flush()
                p1 = UtteranceMappingProvenance(
                    mapping_id=m1.id,
                    source_provenance_json=proof,
                )
                p2 = UtteranceMappingProvenance(
                    mapping_id=m2.id,
                    source_provenance_json=proof,
                )
                db.session.add_all([p1, p2])
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                s1_id = int(s1.id)
                q1_id = int(q1.id)
                m1_id = int(m1.id)
                m2_id = int(m2.id)

                state = inspect_db(db_path, project_id)
                failures += check(
                    "current proven AI mapping generation is readiness-valid",
                    not state.blockers
                    and state.checked_interview_count == 1
                    and state.ai_mapping_count == 2,
                    f"blockers={state.blockers!r}",
                )

                report = final_readiness.audit_final(
                    db_path,
                    outputs,
                    backups,
                    uploads,
                    project_id=project_id,
                )
                final_codes = {str(item.get("code")) for item in report.get("blockers", [])}
                failures += check(
                    "final readiness runs mapping provenance inside hardened audit",
                    report.get("info", {}).get("mapping_provenance_checked_interview_count") == 1
                    and report.get("info", {}).get("mapping_provenance_ai_mapping_count") == 2
                    and not any(
                        code.startswith("ai_mapping_") or code.startswith("mapping_provenance_")
                        for code in final_codes
                    ),
                    f"info={report.get('info', {})!r} mapping_codes={sorted(final_codes)!r}",
                )

                segment = db.session.get(Segment, s1_id)
                segment.text = "edited after mapping"
                db.session.commit()
                state = inspect_db(db_path, project_id)
                failures += check(
                    "post-generation segment edit blocks readiness",
                    "ai_mapping_source_provenance_stale" in blocker_codes(state),
                    f"blockers={state.blockers!r}",
                )

                segment.text = "first answer"
                db.session.commit()
                state = inspect_db(db_path, project_id)
                failures += check(
                    "restoring exact canonical segment bytes restores proof currentness",
                    not state.blockers,
                    f"blockers={state.blockers!r}",
                )

                question = db.session.get(InterviewFlowQuestion, q1_id)
                question.question_text = "Edited first question"
                db.session.commit()
                state = inspect_db(db_path, project_id)
                failures += check(
                    "post-generation question edit blocks readiness",
                    "ai_mapping_source_provenance_stale" in blocker_codes(state),
                    f"blockers={state.blockers!r}",
                )

                question.question_text = "First question"
                db.session.commit()

                db.session.delete(db.session.get(UtteranceMappingProvenance, m1_id))
                db.session.delete(db.session.get(UtteranceMappingProvenance, m2_id))
                db.session.commit()
                state = inspect_db(db_path, project_id)
                failures += check(
                    "AI mapping without generation proof blocks readiness",
                    "ai_mapping_source_provenance_missing" in blocker_codes(state),
                    f"blockers={state.blockers!r}",
                )

                db.session.add_all([
                    UtteranceMappingProvenance(
                        mapping_id=m1_id,
                        source_provenance_json=proof,
                    ),
                    UtteranceMappingProvenance(
                        mapping_id=m2_id,
                        source_provenance_json=proof,
                    ),
                ])
                db.session.commit()

                mixed = json.loads(proof)
                mixed["sha256"] = "0" * 64
                db.session.get(UtteranceMappingProvenance, m2_id).source_provenance_json = json.dumps(
                    mixed,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                db.session.commit()
                state = inspect_db(db_path, project_id)
                failures += check(
                    "mixed AI source generations block readiness",
                    "ai_mapping_mixed_source_provenance" in blocker_codes(state),
                    f"blockers={state.blockers!r}",
                )

                db.session.get(UtteranceMappingProvenance, m2_id).source_provenance_json = proof
                db.session.get(UtteranceMapping, m2_id).mapped_by = "human"
                db.session.commit()
                state = inspect_db(db_path, project_id)
                failures += check(
                    "human override may coexist with remaining current AI mappings",
                    not state.blockers
                    and state.checked_interview_count == 1
                    and state.ai_mapping_count == 1,
                    f"blockers={state.blockers!r} ai_count={state.ai_mapping_count}",
                )

                db.session.remove()
                db.engine.dispose()

            con = sqlite3.connect(db_path)
            try:
                con.execute("DROP TABLE utterance_mapping_provenance")
                con.commit()
            finally:
                con.close()
            state = inspect_db(db_path, project_id)
            failures += check(
                "missing provenance sidecar with AI mappings blocks readiness",
                "mapping_provenance_table_missing" in blocker_codes(state),
                f"blockers={state.blockers!r}",
            )

        except Exception as exc:
            failures += check(
                "mapping provenance readiness smoke",
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
