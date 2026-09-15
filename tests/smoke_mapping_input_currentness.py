from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def blocker_reason(report, interview_id: int) -> str:
    for item in report.blockers:
        context = item.get("context") or {}
        if int(context.get("interview_id") or 0) == int(interview_id):
            return str(context.get("reason") or "")
    return ""


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

    with tempfile.TemporaryDirectory(prefix="qualia_mapping_input_currentness_") as tmp:
        root = Path(tmp)
        db_path = root / "mapping-currentness.db"
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
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.project import Project
            from models.segment import Segment, UtteranceMapping, UtteranceMappingProvenance
            from services.analysis_source_provenance import (
                AnalysisSourceProvenanceError,
                capture_analysis_source_provenance,
                source_provenance_matches_scope,
            )
            import services.analyzer as analyzer_service
            from services.mapping_input_guard import current_mapping_input_status
            from services.mapping_input_readiness_sqlite import inspect_mapping_input_currentness
            from services.mapping_source_provenance import (
                build_mapping_source_manifest,
                mapping_source_provenance_for_manifest,
                serialize_mapping_source_provenance,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Mapping input currentness")
                db.session.add(project)
                db.session.flush()

                flow = InterviewFlow(project_id=project.id, title="Flow")
                other_flow = InterviewFlow(project_id=project.id, title="Other Flow")
                db.session.add_all([flow, other_flow])
                db.session.flush()

                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=0)
                other_section = InterviewFlowSection(
                    flow_id=other_flow.id,
                    title="Other Section",
                    seq=0,
                )
                db.session.add_all([section, other_section])
                db.session.flush()

                q1 = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="質問1",
                    seq=0,
                )
                q2 = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q2",
                    question_text="質問2",
                    seq=1,
                )
                foreign_q = InterviewFlowQuestion(
                    section_id=other_section.id,
                    question_code="X1",
                    question_text="別フロー質問",
                    seq=0,
                )
                db.session.add_all([q1, q2, foreign_q])
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
                    text="質問1への回答",
                    seq=0,
                )
                s2 = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="質問2への回答",
                    seq=1,
                )
                db.session.add_all([s1, s2])
                db.session.flush()

                manifest = build_mapping_source_manifest(interview.id)
                proof = serialize_mapping_source_provenance(
                    mapping_source_provenance_for_manifest(manifest)
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
                db.session.add_all([
                    UtteranceMappingProvenance(
                        mapping_id=m1.id,
                        source_provenance_json=proof,
                    ),
                    UtteranceMappingProvenance(
                        mapping_id=m2.id,
                        source_provenance_json=proof,
                    ),
                ])
                db.session.commit()

                current = current_mapping_input_status(interview.id)
                failures += check(
                    "complete current AI mapping is accepted as analysis input",
                    current.current
                    and current.ai_mapping_count == 2
                    and current.human_mapping_count == 0,
                    str(current),
                )

                con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    sqlite_report = inspect_mapping_input_currentness(
                        con,
                        project_id=project.id,
                    )
                finally:
                    con.close()
                failures += check(
                    "ORM and read-only SQLite currentness agree for current AI mapping",
                    sqlite_report.invalid_count == 0
                    and sqlite_report.current_count == 1,
                    str(sqlite_report),
                )

                analysis_provenance = capture_analysis_source_provenance(
                    "per_question",
                    project.id,
                    interview_id=interview.id,
                    question_id=q1.id,
                )

                # Human review may replace one AI classification. The remaining
                # AI row is still tied to the same current source generation.
                m2 = db.session.get(UtteranceMapping, int(m2.id))
                m2.mapped_by = "human"
                m2.confidence = 1.0
                db.session.commit()
                mixed = current_mapping_input_status(interview.id)
                failures += check(
                    "mixed human and current AI mapping is accepted",
                    mixed.current
                    and mixed.ai_mapping_count == 1
                    and mixed.human_mapping_count == 1,
                    str(mixed),
                )

                con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    mixed_sqlite = inspect_mapping_input_currentness(con)
                finally:
                    con.close()
                failures += check(
                    "SQLite readiness accepts the same mixed human and current AI mapping",
                    mixed_sqlite.invalid_count == 0
                    and mixed_sqlite.current_count == 1,
                    str(mixed_sqlite),
                )

                # A canonical source edit makes the still-AI classification stale.
                s1 = db.session.get(Segment, int(s1.id))
                s1.text = "質問1への修正後回答"
                db.session.commit()
                stale = current_mapping_input_status(interview.id)
                failures += check(
                    "source drift makes remaining AI mapping stale",
                    not stale.current and "canonical source changed" in stale.reason,
                    str(stale),
                )

                provider_calls = {"count": 0}
                original_call = analyzer_service.call_structured

                def forbidden_provider(*_args, **_kwargs):
                    provider_calls["count"] += 1
                    raise AssertionError("provider should not be called for stale mapping input")

                analyzer_service.call_structured = forbidden_provider
                raised = False
                try:
                    analyzer_service.analyze_per_question(
                        interview.id,
                        q1.id,
                        result_write_guard=lambda: None,
                    )
                except AnalysisSourceProvenanceError as exc:
                    raised = "mapping input is stale or unprovable" in str(exc)
                finally:
                    analyzer_service.call_structured = original_call
                failures += check(
                    "stale AI mapping is rejected before analysis provider work",
                    raised and provider_calls["count"] == 0,
                    f"raised={raised} provider_calls={provider_calls['count']}",
                )

                con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    stale_sqlite = inspect_mapping_input_currentness(con)
                finally:
                    con.close()
                failures += check(
                    "readiness blocks the same stale mapping generation",
                    stale_sqlite.invalid_count == 1
                    and "canonical source changed" in blocker_reason(
                        stale_sqlite,
                        interview.id,
                    ),
                    str(stale_sqlite),
                )

                # Restore source. The saved analysis provenance becomes current
                # again because mapping proof and selected segments are unchanged.
                s1.text = "質問1への回答"
                db.session.commit()
                matches, reason = source_provenance_matches_scope(
                    analysis_provenance,
                    "per_question",
                    project.id,
                    interview_id=interview.id,
                    question_id=q1.id,
                )
                failures += check(
                    "analysis provenance is current when its mapping proof is current",
                    matches,
                    reason,
                )

                # Removing the remaining AI proof must invalidate existing
                # mapping-dependent analysis currentness even when mapping rows and
                # source text themselves are unchanged.
                proof_row = db.session.get(UtteranceMappingProvenance, int(m1.id))
                db.session.delete(proof_row)
                db.session.commit()
                matches, reason = source_provenance_matches_scope(
                    analysis_provenance,
                    "per_question",
                    project.id,
                    interview_id=interview.id,
                    question_id=q1.id,
                )
                failures += check(
                    "analysis currentness fails when its AI mapping proof disappears",
                    not matches and "mapping input is stale or unprovable" in reason,
                    reason,
                )

                # Restore proof before testing mapping-shape failures.
                db.session.add(UtteranceMappingProvenance(
                    mapping_id=m1.id,
                    source_provenance_json=proof,
                ))
                db.session.commit()

                # Partial mapping sets are unsafe downstream.
                m2 = db.session.get(UtteranceMapping, int(m2.id))
                db.session.delete(m2)
                db.session.commit()
                partial = current_mapping_input_status(interview.id)
                failures += check(
                    "partial mapping coverage is rejected",
                    not partial.current and "does not cover" in partial.reason,
                    str(partial),
                )

                # Restore a human row, then create a duplicate for the same segment.
                m2 = UtteranceMapping(
                    segment_id=s2.id,
                    question_id=q2.id,
                    mapped_by="human",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add(m2)
                db.session.commit()
                duplicate = UtteranceMapping(
                    segment_id=s2.id,
                    question_id=q2.id,
                    mapped_by="human",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add(duplicate)
                db.session.commit()
                duplicate_status = current_mapping_input_status(interview.id)
                failures += check(
                    "duplicate mapping rows are rejected",
                    not duplicate_status.current and "duplicate rows" in duplicate_status.reason,
                    str(duplicate_status),
                )
                db.session.delete(duplicate)
                db.session.commit()

                # A human mapping is canonical, but it still cannot point outside
                # the interview's selected flow.
                m2 = db.session.get(UtteranceMapping, int(m2.id))
                m2.question_id = foreign_q.id
                db.session.commit()
                out_of_flow = current_mapping_input_status(interview.id)
                failures += check(
                    "human mapping outside the interview flow is rejected",
                    not out_of_flow.current and "outside the interview flow" in out_of_flow.reason,
                    str(out_of_flow),
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "mapping input currentness smoke",
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
