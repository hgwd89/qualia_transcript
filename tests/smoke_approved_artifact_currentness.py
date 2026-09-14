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
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.analysis_source_provenance import capture_analysis_source_provenance
            from services.file_manager import (
                open_output_target_for_write,
                prepare_output_target,
                register_generated_file,
            )

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
                participant_id = int(participant.id)
                provenance = capture_analysis_source_provenance(
                    "per_question",
                    project_id,
                    interview_id=int(interview.id),
                    question_id=int(question.id),
                )
                analysis = AIAnalysis(
                    project_id=project_id,
                    interview_id=int(interview.id),
                    question_id=int(question.id),
                    analysis_type="per_question",
                    title="Q1 考察",
                    summary_text="安心感が重要",
                    content_json=json.dumps({
                        "question_id": int(question.id),
                        "question_code": "Q1",
                        "question_text": question.question_text,
                        "findings": [{
                            "point": "安心感が重要",
                            "evidence_quote": source_text,
                            "source_segment_ids": [int(segment.id)],
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

                params = {
                    "approved_only": True,
                    "analysis_count": 1,
                    "finding_count": 1,
                    "analysis_ids": [analysis_id],
                    "source_provenance_sha256": {
                        str(analysis_id): provenance["sha256"],
                    },
                }
                formal = register_bytes(
                    "承認済AI分析_current.xlsx",
                    "approved_analysis",
                    b"formal-current",
                    params,
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
                    response.status_code == 200 and response.data == b"formal-current",
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
                    "legacy formal artifact without generation provenance is not distributable",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

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

                second_provenance = capture_analysis_source_provenance(
                    "per_question",
                    project_id,
                    interview_id=int(interview.id),
                    question_id=int(question.id),
                )
                second = AIAnalysis(
                    project_id=project_id,
                    interview_id=int(interview.id),
                    question_id=int(question.id),
                    analysis_type="per_question",
                    title="Q1 second",
                    summary_text="second",
                    content_json=json.dumps({
                        "findings": [{
                            "point": "second",
                            "evidence_quote": source_text,
                            "source_segment_ids": [int(segment.id)],
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

                response = client.get(f"/api/outputs/{formal_id}/download")
                failures += check(
                    "formal artifact becomes historical when current approved set changes",
                    response.status_code == 409,
                    f"status={response.status_code}",
                )
                response.close()

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
