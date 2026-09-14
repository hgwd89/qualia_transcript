import hashlib
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
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    scripts_dir = repo_root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_approved_artifact_currentness_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'artifact_currentness.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")

        try:
            from app import create_app
            from audit_production_readiness_v2 import _formal_artifact_hash_reason
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.analysis_review import set_analysis_review_status
            from services.analysis_source_provenance import capture_analysis_source_provenance
            from services.approved_analysis_currentness import (
                formal_analysis_state_sha256,
                formal_artifact_expected_sha256,
            )
            from services.file_manager import (
                open_output_target_for_write,
                prepare_output_target,
                register_generated_file,
            )
            from services.formal_artifact_integrity import verified_artifact_snapshot
            from services.storage_paths import open_managed_file_for_read

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Formal artifact currentness")
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
                db.session.add(UtteranceMapping(
                    segment_id=segment.id,
                    question_id=question.id,
                    mapped_by="manual",
                    confidence=1.0,
                    is_unclassified=False,
                ))
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                question_id = int(question.id)
                participant_id = int(participant.id)
                segment_id = int(segment.id)
                provenance = capture_analysis_source_provenance(
                    "per_question",
                    project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                )
                analysis = AIAnalysis(
                    project_id=project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                    analysis_type="per_question",
                    title="Q1 考察",
                    summary_text="安心感が重要",
                    content_json=json.dumps({
                        "question_id": question_id,
                        "question_code": "Q1",
                        "question_text": question.question_text,
                        "findings": [{
                            "point": "安心感が重要",
                            "evidence_quote": source_text,
                            "source_segment_ids": [segment_id],
                            "participant_codes": ["P01"],
                            "question_codes": ["Q1"],
                            "confidence": "high",
                        }],
                        "implications": "安心感を明示する",
                        "unresolved": "",
                        "source_provenance": provenance,
                    }, ensure_ascii=False),
                    model_used="test",
                    review_status="approved",
                )
                db.session.add(analysis)
                db.session.commit()
                analysis_id = int(analysis.id)

                def register_bytes(name: str, file_type: str, data: bytes, params=None):
                    target = prepare_output_target(project_id, name)
                    opened = open_output_target_for_write(target)
                    try:
                        opened.stream.write(data)
                    finally:
                        opened.close()
                    return register_generated_file(
                        target,
                        project_id=project_id,
                        file_type=file_type,
                        file_format="xlsx",
                        generation_params_json=(
                            json.dumps(params, ensure_ascii=False) if params is not None else None
                        ),
                    )

                def formal_params(current_analysis: AIAnalysis, data: bytes) -> dict:
                    current_content = json.loads(current_analysis.content_json or "{}")
                    current_provenance = current_content.get("source_provenance") or {}
                    aid = int(current_analysis.id)
                    return {
                        "approved_only": True,
                        "analysis_count": 1,
                        "finding_count": 1,
                        "analysis_ids": [aid],
                        "source_provenance_sha256": {
                            str(aid): str(current_provenance.get("sha256") or ""),
                        },
                        "formal_analysis_state_sha256": {
                            str(aid): formal_analysis_state_sha256(current_analysis),
                        },
                        "artifact_sha256": hashlib.sha256(data).hexdigest(),
                    }

                formal_bytes = b"formal-current"
                formal = register_bytes(
                    "承認済AI分析_current.xlsx",
                    "approved_analysis",
                    formal_bytes,
                    formal_params(analysis, formal_bytes),
                )
                ordinary = register_bytes(
                    "analysis.xlsx",
                    "analysis",
                    b"ordinary",
                )
                legacy_formal = register_bytes(
                    "承認済AI分析_legacy.xlsx",
                    "approved_analysis",
                    b"legacy-formal",
                )

                formal_id = int(formal.id)
                ordinary_id = int(ordinary.id)
                legacy_formal_id = int(legacy_formal.id)
                client = app.test_client()

                response = client.get(f"/api/outputs/{formal_id}/download")
                failures += check(
                    "current formal artifact downloads exact registered bytes",
                    response.status_code == 200 and response.data == formal_bytes,
                    f"status={response.status_code} data={response.data!r}",
                )
                response.close()

                page = client.get(f"/projects/{project_id}/outputs")
                page_text = page.get_data(as_text=True)
                failures += check(
                    "outputs UI reports current approved analysis and current formal artifact",
                    page.status_code == 200
                    and "現在有効 1 / 承認状態 1" in page_text
                    and "現在有効" in page_text,
                    f"status={page.status_code}",
                )
                page.close()

                response = client.get(f"/api/outputs/{legacy_formal_id}/download")
                failures += check(
                    "legacy formal artifact without complete provenance/state/hash metadata is not distributable",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

                legacy_reason = _formal_artifact_hash_reason(
                    {
                        "generation_params_json": legacy_formal.generation_params_json,
                        "stored_path": legacy_formal.stored_path,
                    },
                    Path(config.OUTPUT_DIR),
                )
                failures += check(
                    "readiness fails closed for legacy formal artifact without byte hash",
                    bool(legacy_reason) and "SHA-256" in legacy_reason,
                    f"reason={legacy_reason}",
                )

                participant = db.session.get(Participant, participant_id)
                participant.participant_code = "P99"
                db.session.commit()

                response = client.get(f"/api/outputs/{formal_id}/download")
                failures += check(
                    "source drift blocks a previously generated formal artifact download",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

                ordinary_response = client.get(f"/api/outputs/{ordinary_id}/download")
                failures += check(
                    "source drift does not affect ordinary generated-file downloads",
                    ordinary_response.status_code == 200 and ordinary_response.data == b"ordinary",
                    f"status={ordinary_response.status_code}",
                )
                ordinary_response.close()

                stale_page = client.get(f"/projects/{project_id}/outputs")
                stale_text = stale_page.get_data(as_text=True)
                failures += check(
                    "outputs UI marks stale formal artifacts non-distributable and disables formal generation",
                    stale_page.status_code == 200
                    and "現在有効 0 / 承認状態 1" in stale_text
                    and "履歴・配布不可" in stale_text
                    and 'id="btn-approved-analysis"' in stale_text
                    and "disabled" in stale_text,
                    f"status={stale_page.status_code}",
                )
                stale_page.close()

                participant = db.session.get(Participant, participant_id)
                participant.participant_code = "P01"
                db.session.commit()

                analysis = db.session.get(AIAnalysis, analysis_id)
                analysis, unresolved = set_analysis_review_status(
                    analysis,
                    "approved",
                    "review metadata changed",
                )
                failures += check(
                    "reapproval with current source succeeds",
                    not unresolved and analysis.review_status == "approved",
                    f"unresolved={unresolved}",
                )

                response = client.get(f"/api/outputs/{formal_id}/download")
                failures += check(
                    "review-state drift blocks an older formal artifact even when source is unchanged",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

                refreshed_bytes = b"formal-refreshed"
                refreshed = register_bytes(
                    "承認済AI分析_refreshed.xlsx",
                    "approved_analysis",
                    refreshed_bytes,
                    formal_params(analysis, refreshed_bytes),
                )
                refreshed_id = int(refreshed.id)
                response = client.get(f"/api/outputs/{refreshed_id}/download")
                failures += check(
                    "refreshed formal artifact is current after review metadata change",
                    response.status_code == 200 and response.data == refreshed_bytes,
                    f"status={response.status_code}",
                )
                response.close()

                readiness_row = {
                    "generation_params_json": refreshed.generation_params_json,
                    "stored_path": refreshed.stored_path,
                }
                readiness_reason = _formal_artifact_hash_reason(
                    readiness_row,
                    Path(config.OUTPUT_DIR),
                )
                failures += check(
                    "readiness accepts the same current formal bytes as delivery",
                    readiness_reason is None,
                    f"reason={readiness_reason}",
                )

                refreshed_path = Path(config.OUTPUT_DIR) / refreshed.stored_path
                expected_hash = formal_artifact_expected_sha256(refreshed)
                managed = open_managed_file_for_read(config.OUTPUT_DIR, refreshed.stored_path)
                verified = verified_artifact_snapshot(managed, expected_hash)
                try:
                    with refreshed_path.open("r+b") as mutated:
                        mutated.seek(0)
                        mutated.write(b"X" * len(refreshed_bytes))
                        mutated.flush()
                    verified.stream.seek(0)
                    snapshotted = verified.stream.read()
                finally:
                    verified.close()
                failures += check(
                    "verified formal snapshot is immutable after later in-place managed-file mutation",
                    snapshotted == refreshed_bytes,
                    f"snapshot={snapshotted!r}",
                )

                response = client.get(f"/api/outputs/{refreshed_id}/download")
                failures += check(
                    "in-place formal artifact byte tampering is rejected",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

                readiness_reason = _formal_artifact_hash_reason(
                    readiness_row,
                    Path(config.OUTPUT_DIR),
                )
                failures += check(
                    "readiness rejects the same tampered formal bytes as delivery",
                    bool(readiness_reason) and "do not match" in readiness_reason,
                    f"reason={readiness_reason}",
                )

                with refreshed_path.open("r+b") as restored:
                    restored.seek(0)
                    restored.write(refreshed_bytes)
                    restored.truncate(len(refreshed_bytes))
                    restored.flush()
                response = client.get(f"/api/outputs/{refreshed_id}/download")
                failures += check(
                    "restoring exact registered formal bytes restores distributability",
                    response.status_code == 200 and response.data == refreshed_bytes,
                    f"status={response.status_code}",
                )
                response.close()

                readiness_reason = _formal_artifact_hash_reason(
                    readiness_row,
                    Path(config.OUTPUT_DIR),
                )
                failures += check(
                    "restoring exact registered formal bytes restores readiness byte acceptance",
                    readiness_reason is None,
                    f"reason={readiness_reason}",
                )

                second_provenance = capture_analysis_source_provenance(
                    "per_question",
                    project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                )
                second = AIAnalysis(
                    project_id=project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                    analysis_type="per_question",
                    title="Q1 second",
                    summary_text="second",
                    content_json=json.dumps({
                        "findings": [{
                            "point": "second",
                            "evidence_quote": source_text,
                            "source_segment_ids": [segment_id],
                            "participant_codes": ["P01"],
                            "question_codes": ["Q1"],
                            "confidence": "high",
                        }],
                        "implications": "second",
                        "unresolved": "",
                        "source_provenance": second_provenance,
                    }, ensure_ascii=False),
                    model_used="test",
                    review_status="approved",
                )
                db.session.add(second)
                db.session.commit()

                response = client.get(f"/api/outputs/{refreshed_id}/download")
                failures += check(
                    "formal artifact becomes historical when current approved set changes",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

                report_source = (repo_root / "services" / "report_approved_analysis.py").read_text(
                    encoding="utf-8"
                )
                failures += check(
                    "formal exporter records source, analysis-state, and artifact byte hashes",
                    '"source_provenance_sha256"' in report_source
                    and '"formal_analysis_state_sha256"' in report_source
                    and '"artifact_sha256"' in report_source,
                )

                db.session.rollback()
                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
