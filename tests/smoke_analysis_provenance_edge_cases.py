import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def blocker_codes(report: dict) -> list[str]:
    return [str(item.get("code") or "") for item in report.get("blockers", [])]


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_analysis_provenance_edges_") as tmp:
        root = Path(tmp)
        db_path = root / "provenance_edges.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(output_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.analysis_review import set_analysis_review_status
            from services.analysis_source_provenance import AnalysisSourceProvenanceError
            from scripts.audit_production_readiness import audit
            import services.analyzer as analyzer

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(
                    name="Provenance edge smoke",
                    client="Smoke Client",
                    research_objective="Verify duplicate mappings and readiness provenance",
                )
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Main flow")
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
                    participant_code="P01",
                    display_name="Participant 1",
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
                source_text = "安心して使えることが一番重要です。"
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
                mapping = UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                )
                duplicate = UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add_all([mapping, duplicate])
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                question_id = int(question.id)
                participant_id = int(participant.id)
                segment_id = int(segment.id)
                duplicate_id = int(duplicate.id)

                prompts: list[str] = []
                original_call = analyzer.call_structured

                def provider(_system, user, *_args, **_kwargs):
                    prompts.append(str(user))
                    return {
                        "findings": [{
                            "point": "安心感が重要",
                            "evidence_quote": source_text,
                            "participant_codes": ["P01"],
                            "question_codes": ["Q1"],
                            "confidence": "high",
                        }],
                        "implications": "安心感を明示する",
                        "unresolved": "",
                    }

                def result_write_guard():
                    if db.session.new or db.session.dirty or db.session.deleted:
                        raise AssertionError("test guard requires clean session")
                    db.session.rollback()
                    db.session.execute(text("BEGIN IMMEDIATE"))

                analyzer.call_structured = provider
                try:
                    analysis = analyzer.analyze_per_question(
                        interview_id,
                        question_id,
                        result_write_guard=result_write_guard,
                    )
                finally:
                    analyzer.call_structured = original_call

                prompt = prompts[-1] if prompts else ""
                failures += check(
                    "duplicate mappings do not duplicate provider source text",
                    prompt.count(source_text) == 1,
                    f"source_occurrences={prompt.count(source_text)}",
                )

                db.session.delete(db.session.get(UtteranceMapping, duplicate_id))
                db.session.commit()
                analysis, unresolved = set_analysis_review_status(analysis, "approved")
                failures += check(
                    "removing duplicate mapping does not stale deduped analysis",
                    not unresolved and analysis.review_status == "approved",
                    f"status={analysis.review_status} unresolved={unresolved}",
                )

                db.session.remove()
                db.engine.dispose()
                report = audit(db_path, output_dir, backup_dir)
                failures += check(
                    "readiness accepts current approved generation provenance",
                    "approved_analysis_source_provenance_invalid" not in blocker_codes(report),
                    f"blockers={blocker_codes(report)}",
                )

                participant = db.session.get(Participant, participant_id)
                participant.participant_code = "P99"
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
                stale_report = audit(db_path, output_dir, backup_dir)
                failures += check(
                    "readiness blocks stale approved generation provenance",
                    "approved_analysis_source_provenance_invalid" in blocker_codes(stale_report),
                    f"blockers={blocker_codes(stale_report)}",
                )

                participant = db.session.get(Participant, participant_id)
                participant.participant_code = "P01"
                db.session.commit()

                before_count = AIAnalysis.query.count()
                original_source_reader = analyzer._mapped_respondent_segments
                source_reader_mutated = {"value": False}

                def mutating_source_reader(*args, **kwargs):
                    if not source_reader_mutated["value"]:
                        source_reader_mutated["value"] = True
                        current = db.session.get(Segment, segment_id)
                        current.text = source_text + " 読取時変更"
                        db.session.commit()
                    return original_source_reader(*args, **kwargs)

                analyzer._mapped_respondent_segments = mutating_source_reader
                analyzer.call_structured = provider
                prompt_snapshot_rejected = False
                prompt_snapshot_error = ""
                try:
                    analyzer.analyze_per_question(
                        interview_id,
                        question_id,
                        result_write_guard=result_write_guard,
                    )
                except AnalysisSourceProvenanceError as exc:
                    prompt_snapshot_rejected = True
                    prompt_snapshot_error = str(exc)
                    db.session.rollback()
                finally:
                    analyzer._mapped_respondent_segments = original_source_reader
                    analyzer.call_structured = original_call

                failures += check(
                    "generation fingerprint is captured before prompt source reads",
                    prompt_snapshot_rejected
                    and source_reader_mutated["value"]
                    and AIAnalysis.query.count() == before_count
                    and "changed after generation" in prompt_snapshot_error,
                    f"rejected={prompt_snapshot_rejected} mutated={source_reader_mutated['value']} analyses={AIAnalysis.query.count()} error={prompt_snapshot_error}",
                )

                current = db.session.get(Segment, segment_id)
                current.text = source_text
                db.session.commit()

                legacy = AIAnalysis(
                    project_id=project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                    analysis_type="per_question",
                    title="Legacy approved row",
                    summary_text="legacy",
                    content_json=json.dumps({
                        "findings": [{
                            "point": "legacy",
                            "evidence_quote": source_text,
                            "source_segment_ids": [segment_id],
                            "participant_codes": ["P01"],
                            "question_codes": ["Q1"],
                            "confidence": "high",
                        }],
                        "implications": "legacy",
                        "unresolved": "",
                    }, ensure_ascii=False),
                    model_used="legacy-test",
                    review_status="approved",
                )
                db.session.add(legacy)
                db.session.commit()
                legacy_id = int(legacy.id)
                db.session.remove()
                db.engine.dispose()
                legacy_report = audit(db_path, output_dir, backup_dir)
                provenance_blockers = [
                    item for item in legacy_report.get("blockers", [])
                    if item.get("code") == "approved_analysis_source_provenance_invalid"
                ]
                legacy_blocked = any(
                    int((item.get("context") or {}).get("analysis_id") or 0) == legacy_id
                    and "no generation source provenance" in str((item.get("context") or {}).get("reason") or "")
                    for item in provenance_blockers
                )
                failures += check(
                    "readiness blocks legacy approved analysis without provenance",
                    legacy_blocked,
                    f"provenance_blockers={provenance_blockers}",
                )

                db.session.rollback()
                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
