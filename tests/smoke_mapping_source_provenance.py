from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
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

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_mapping_provenance_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'mapping-provenance.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment, UtteranceMapping, UtteranceMappingProvenance
            import services.mapper as mapper_service
            from services.mapping_source_provenance import (
                MappingSourceProvenanceError,
                mapping_source_provenance_status,
                validate_current_ai_mapping_batch,
            )
            from services.processing_result_guard import find_completed_mapping_count_for_job

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Mapping provenance")
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
                    question_text="最初の質問",
                    seq=0,
                )
                db.session.add(question)
                db.session.flush()

                interview = Interview(
                    project_id=project.id,
                    flow_id=flow.id,
                    status="transcribed",
                )
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="生成時の回答",
                    seq=0,
                )
                db.session.add(segment)
                db.session.flush()

                prior = UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="human",
                    confidence=1.0,
                    is_unclassified=False,
                    notes="must_survive_stale_provider",
                )
                db.session.add(prior)
                db.session.commit()

                payload = [{
                    "segment_id": int(segment.id),
                    "question_id": int(question.id),
                    "confidence": 0.95,
                    "is_unclassified": False,
                }]
                original_call = mapper_service.call_structured

                # Mutate canonical source while provider work is in flight. The
                # provider result belongs to the old manifest and must not replace
                # the prior canonical mapping generation.
                def mutating_call(_system, _user, *_args, **_kwargs):
                    current_segment = db.session.get(Segment, int(segment.id))
                    current_segment.text = "provider実行中に変更された回答"
                    db.session.commit()
                    return {"mappings": payload}

                mapper_service.call_structured = mutating_call
                raised = False
                try:
                    mapper_service.run_mapping(
                        interview.id,
                        result_write_guard=lambda: None,
                    )
                except MappingSourceProvenanceError as exc:
                    raised = "canonical source changed" in str(exc)
                finally:
                    mapper_service.call_structured = original_call

                preserved = UtteranceMapping.query.filter_by(segment_id=segment.id).all()
                failures += check(
                    "provider-time source mutation is rejected before replacement",
                    raised
                    and len(preserved) == 1
                    and preserved[0].mapped_by == "human"
                    and preserved[0].notes == "must_survive_stale_provider",
                    f"raised={raised} rows={len(preserved)}",
                )

                # Restore source, establish a running durable attempt, and create a
                # valid generation with a providerless deterministic result.
                segment = db.session.get(Segment, int(segment.id))
                segment.text = "生成時の回答"
                interview = db.session.get(Interview, int(interview.id))
                interview.status = "transcribed"
                job = ProcessingJob(
                    project_id=project.id,
                    interview_id=interview.id,
                    job_type="map",
                    status="running",
                    attempt_count=1,
                    started_at=datetime.now(timezone.utc),
                )
                db.session.add(job)
                db.session.commit()

                captured_user = {"value": ""}

                def stable_call(_system, user, *_args, **_kwargs):
                    captured_user["value"] = user
                    return {"mappings": payload}

                mapper_service.call_structured = stable_call
                try:
                    mapped_count = mapper_service.run_mapping(
                        interview.id,
                        result_write_guard=lambda: None,
                    )
                finally:
                    mapper_service.call_structured = original_call

                mapping = UtteranceMapping.query.filter_by(segment_id=segment.id).one()
                proof_row = db.session.get(UtteranceMappingProvenance, int(mapping.id))
                current, reason, batch_count = validate_current_ai_mapping_batch(interview.id)
                failures += check(
                    "AI mapping persists one current source proof",
                    mapped_count == 1
                    and mapping.mapped_by == "ai"
                    and proof_row is not None
                    and current
                    and batch_count == 1
                    and "生成時の回答" in captured_user["value"]
                    and "最初の質問" in captured_user["value"],
                    f"mapped={mapped_count} current={current} reason={reason}",
                )
                failures += check(
                    "current-attempt crash recovery accepts current generation",
                    find_completed_mapping_count_for_job(job) == 1,
                )

                proof = proof_row.source_provenance_json
                proof_current, proof_reason = mapping_source_provenance_status(
                    proof,
                    interview_id=interview.id,
                )
                failures += check(
                    "persisted proof independently validates against canonical source",
                    proof_current,
                    proof_reason,
                )

                segment = db.session.get(Segment, int(segment.id))
                segment.text = "完了後に修正された回答"
                db.session.commit()
                stale, stale_reason, _count = validate_current_ai_mapping_batch(interview.id)
                failures += check(
                    "post-completion segment edit makes mapping generation stale",
                    not stale and "canonical source changed" in stale_reason,
                    stale_reason,
                )
                failures += check(
                    "crash recovery refuses mapping after segment source drift",
                    find_completed_mapping_count_for_job(job) is None,
                )

                segment.text = "生成時の回答"
                db.session.commit()
                question = db.session.get(InterviewFlowQuestion, int(question.id))
                question.question_text = "完了後に修正された質問"
                db.session.commit()
                stale, stale_reason, _count = validate_current_ai_mapping_batch(interview.id)
                failures += check(
                    "post-completion question edit makes mapping generation stale",
                    not stale and "canonical source changed" in stale_reason,
                    stale_reason,
                )

                question.question_text = "最初の質問"
                db.session.commit()
                proof_row = db.session.get(UtteranceMappingProvenance, int(mapping.id))
                db.session.delete(proof_row)
                db.session.commit()
                current, reason, _count = validate_current_ai_mapping_batch(interview.id)
                failures += check(
                    "legacy AI mapping without provenance fails closed",
                    not current and "missing or malformed" in reason,
                    reason,
                )
                failures += check(
                    "crash recovery refuses unproven legacy mapping",
                    find_completed_mapping_count_for_job(job) is None,
                )

                # The sidecar table must be created automatically for an existing
                # application schema without ALTERing historical mapping rows.
                table_exists = db.session.execute(db.text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='utterance_mapping_provenance'"
                )).first() is not None
                failures += check(
                    "mapping provenance sidecar schema exists",
                    table_exists,
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "mapping source provenance smoke",
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
