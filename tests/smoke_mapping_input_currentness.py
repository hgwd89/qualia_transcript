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


def sqlite_report(db_path: Path, inspector, project_id: int | None = None):
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return inspector(con, project_id=project_id)
    finally:
        con.close()


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
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        config.BACKUP_DIR = str(root / "backups")

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

                proof = serialize_mapping_source_provenance(
                    mapping_source_provenance_for_manifest(
                        build_mapping_source_manifest(interview.id)
                    )
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

                project_id = int(project.id)
                interview_id = int(interview.id)
                q1_id = int(q1.id)
                q2_id = int(q2.id)
                foreign_q_id = int(foreign_q.id)
                s1_id = int(s1.id)
                s2_id = int(s2.id)
                m1_id = int(m1.id)
                m2_id = int(m2.id)

                current = current_mapping_input_status(interview_id)
                current_sqlite = sqlite_report(
                    db_path,
                    inspect_mapping_input_currentness,
                    project_id,
                )
                failures += check(
                    "complete current AI mapping is accepted by ORM and readiness",
                    current.current
                    and current.ai_mapping_count == 2
                    and current_sqlite.invalid_count == 0
                    and current_sqlite.current_count == 1,
                    f"orm={current} sqlite={current_sqlite}",
                )

                analysis_provenance = capture_analysis_source_provenance(
                    "per_question",
                    project_id,
                    interview_id=interview_id,
                    question_id=q1_id,
                )

                # Human override is canonical and may coexist with still-current
                # AI mappings for other segments.
                m2 = db.session.get(UtteranceMapping, m2_id)
                m2.mapped_by = "human"
                m2.confidence = 1.0
                db.session.commit()
                mixed = current_mapping_input_status(interview_id)
                mixed_sqlite = sqlite_report(db_path, inspect_mapping_input_currentness)
                failures += check(
                    "mixed human and current AI mapping is accepted consistently",
                    mixed.current
                    and mixed.ai_mapping_count == 1
                    and mixed.human_mapping_count == 1
                    and mixed_sqlite.invalid_count == 0,
                    f"orm={mixed} sqlite={mixed_sqlite}",
                )

                # Source drift makes the remaining AI classification stale and
                # must stop analysis before provider work begins.
                s1 = db.session.get(Segment, s1_id)
                s1.text = "質問1への修正後回答"
                db.session.commit()
                stale = current_mapping_input_status(interview_id)
                stale_sqlite = sqlite_report(db_path, inspect_mapping_input_currentness)
                failures += check(
                    "source drift makes AI mapping stale in ORM and readiness",
                    not stale.current
                    and "canonical source changed" in stale.reason
                    and stale_sqlite.invalid_count == 1
                    and "canonical source changed" in blocker_reason(stale_sqlite, interview_id),
                    f"orm={stale} sqlite={stale_sqlite}",
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
                        interview_id,
                        q1_id,
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

                # Restore source and prove the analysis fingerprint is current.
                s1 = db.session.get(Segment, s1_id)
                s1.text = "質問1への回答"
                db.session.commit()
                matches, reason = source_provenance_matches_scope(
                    analysis_provenance,
                    "per_question",
                    project_id,
                    interview_id=interview_id,
                    question_id=q1_id,
                )
                failures += check(
                    "analysis provenance is current while mapping proof is current",
                    matches,
                    reason,
                )

                # Removing the remaining AI sidecar invalidates existing analysis
                # currentness even though rows/text are otherwise unchanged.
                proof_row = db.session.get(UtteranceMappingProvenance, m1_id)
                db.session.delete(proof_row)
                db.session.commit()
                matches, reason = source_provenance_matches_scope(
                    analysis_provenance,
                    "per_question",
                    project_id,
                    interview_id=interview_id,
                    question_id=q1_id,
                )
                failures += check(
                    "analysis currentness fails when its AI mapping proof disappears",
                    not matches and "mapping input is stale or unprovable" in reason,
                    reason,
                )
                db.session.add(UtteranceMappingProvenance(
                    mapping_id=m1_id,
                    source_provenance_json=proof,
                ))
                db.session.commit()

                # Partial coverage is unsafe downstream.
                m2 = db.session.get(UtteranceMapping, m2_id)
                db.session.delete(m2)
                db.session.commit()
                partial = current_mapping_input_status(interview_id)
                failures += check(
                    "partial mapping coverage is rejected",
                    not partial.current and "does not cover" in partial.reason,
                    str(partial),
                )

                # Restore the human row. Identical historical duplicates are safe
                # because prompt/provenance logic already deduplicates by segment.
                m2 = UtteranceMapping(
                    segment_id=s2_id,
                    question_id=q2_id,
                    mapped_by="human",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add(m2)
                db.session.commit()
                duplicate = UtteranceMapping(
                    segment_id=s2_id,
                    question_id=q2_id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add(duplicate)
                db.session.commit()
                identical_duplicate = current_mapping_input_status(interview_id)
                failures += check(
                    "semantically identical historical duplicate mappings remain accepted",
                    identical_duplicate.current,
                    str(identical_duplicate),
                )

                # The same duplicate becomes invalid when it claims a different
                # canonical question for the same segment.
                duplicate.question_id = q1_id
                db.session.commit()
                conflicting = current_mapping_input_status(interview_id)
                conflicting_sqlite = sqlite_report(db_path, inspect_mapping_input_currentness)
                failures += check(
                    "conflicting duplicate mapping assignments are rejected",
                    not conflicting.current
                    and "conflicting duplicate rows" in conflicting.reason
                    and conflicting_sqlite.invalid_count == 1
                    and "conflicting duplicate rows" in blocker_reason(
                        conflicting_sqlite,
                        interview_id,
                    ),
                    f"orm={conflicting} sqlite={conflicting_sqlite}",
                )
                db.session.delete(duplicate)
                db.session.commit()

                # A human mapping is canonical, but cannot point outside the
                # interview's selected flow.
                m2 = db.session.get(UtteranceMapping, int(m2.id))
                m2.question_id = foreign_q_id
                db.session.commit()
                out_of_flow = current_mapping_input_status(interview_id)
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
