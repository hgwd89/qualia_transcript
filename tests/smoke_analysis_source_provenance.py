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

    with tempfile.TemporaryDirectory(prefix="qualia_analysis_provenance_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'analysis_provenance.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.generated_file import GeneratedFile
            from models.interview import Interview
            from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from services.analysis_review import set_analysis_review_status
            from services.analysis_source_provenance import (
                PROVENANCE_KEY,
                PROVENANCE_VERSION,
                AnalysisSourceProvenanceError,
            )
            from services.report_approved_analysis import generate_approved_analysis_xlsx
            import services.analyzer as analyzer

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(
                    name="Analysis provenance smoke",
                    client="Smoke Client",
                    research_objective="Verify generation-time source provenance",
                )
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Main flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section 1", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="最も重要なことは何ですか？",
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
                segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="一番大事なのは安心して使えることです。",
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
                db.session.add(mapping)
                db.session.commit()

                project_id = int(project.id)
                interview_id = int(interview.id)
                question_id = int(question.id)
                participant_id = int(participant.id)
                segment_id = int(segment.id)
                mapping_id = int(mapping.id)
                original_text = str(segment.text)

                def result_write_guard():
                    if db.session.new or db.session.dirty or db.session.deleted:
                        raise AssertionError("test result-write guard requires a clean session")
                    db.session.rollback()
                    db.session.execute(text("BEGIN IMMEDIATE"))

                def provider_result(*_args, **_kwargs):
                    return {
                        "findings": [{
                            "point": "安心感が重要",
                            "evidence_quote": original_text,
                            "participant_codes": ["P01"],
                            "question_codes": ["Q1"],
                            "confidence": "high",
                        }],
                        "implications": "安心感を明示する",
                        "unresolved": "",
                    }

                original_call = analyzer.call_structured
                analyzer.call_structured = provider_result
                try:
                    analysis = analyzer.analyze_per_question(
                        interview_id,
                        question_id,
                        result_write_guard=result_write_guard,
                    )
                    content = json.loads(analysis.content_json)
                    provenance = content.get(PROVENANCE_KEY) or {}
                    failures += check(
                        "generated analysis stores source provenance",
                        provenance.get("version") == PROVENANCE_VERSION
                        and len(str(provenance.get("sha256") or "")) == 64,
                        f"provenance={provenance}",
                    )

                    analysis, unresolved = set_analysis_review_status(analysis, "approved")
                    approved_content = json.loads(analysis.content_json)
                    source_ids = approved_content["findings"][0].get("source_segment_ids") or []
                    failures += check(
                        "current generated analysis can be approved",
                        not unresolved
                        and analysis.review_status == "approved"
                        and source_ids == [segment_id],
                        f"status={analysis.review_status} unresolved={unresolved} source_ids={source_ids}",
                    )

                    participant = db.session.get(Participant, participant_id)
                    participant.participant_code = "P99"
                    db.session.commit()
                    before_files = GeneratedFile.query.count()
                    export_rejected = False
                    export_error = ""
                    try:
                        generate_approved_analysis_xlsx(project_id)
                    except ValueError as exc:
                        export_error = str(exc)
                        export_rejected = "canonical data" in export_error
                    failures += check(
                        "stale approved analysis cannot be formally exported",
                        export_rejected and GeneratedFile.query.count() == before_files,
                        f"rejected={export_rejected} files={GeneratedFile.query.count()} error={export_error}",
                    )

                    participant = db.session.get(Participant, participant_id)
                    participant.participant_code = "P01"
                    db.session.commit()
                    generated = generate_approved_analysis_xlsx(project_id)
                    params = json.loads(generated.generation_params_json or "{}")
                    failures += check(
                        "current approved export records analysis provenance",
                        GeneratedFile.query.count() == before_files + 1
                        and params.get("analysis_ids") == [int(analysis.id)]
                        and params.get("source_provenance_sha256", {}).get(str(int(analysis.id)))
                        == provenance.get("sha256"),
                        f"file_id={generated.id} params={params}",
                    )

                    draft = analyzer.analyze_per_question(
                        interview_id,
                        question_id,
                        result_write_guard=result_write_guard,
                    )
                    mapping = db.session.get(UtteranceMapping, mapping_id)
                    mapping.question_id = None
                    mapping.is_unclassified = True
                    mapping.mapped_by = "human"
                    db.session.commit()
                    draft, unresolved = set_analysis_review_status(draft, "approved")
                    failures += check(
                        "analysis generated before mapping change cannot be approved",
                        bool(unresolved)
                        and draft.review_status == "draft"
                        and unresolved[0].get("reason") == "analysis source provenance is stale or unprovable",
                        f"status={draft.review_status} unresolved={unresolved}",
                    )

                    mapping = db.session.get(UtteranceMapping, mapping_id)
                    mapping.question_id = question_id
                    mapping.is_unclassified = False
                    mapping.mapped_by = "manual"
                    db.session.commit()

                    before_analyses = AIAnalysis.query.count()

                    def mutating_provider(*_args, **_kwargs):
                        source = db.session.get(Segment, segment_id)
                        source.text = original_text + " 変更"
                        db.session.commit()
                        return provider_result()

                    analyzer.call_structured = mutating_provider
                    drift_rejected = False
                    drift_error = ""
                    try:
                        analyzer.analyze_per_question(
                            interview_id,
                            question_id,
                            result_write_guard=result_write_guard,
                        )
                    except AnalysisSourceProvenanceError as exc:
                        drift_rejected = True
                        drift_error = str(exc)
                        db.session.rollback()
                    failures += check(
                        "provider-time source drift is rejected before analysis commit",
                        drift_rejected
                        and AIAnalysis.query.count() == before_analyses
                        and "changed after generation" in drift_error,
                        f"rejected={drift_rejected} analyses={AIAnalysis.query.count()} error={drift_error}",
                    )

                    source = db.session.get(Segment, segment_id)
                    source.text = original_text
                    db.session.commit()

                    legacy = AIAnalysis(
                        project_id=project_id,
                        interview_id=interview_id,
                        question_id=question_id,
                        analysis_type="per_question",
                        title="Legacy analysis without provenance",
                        summary_text="legacy",
                        content_json=json.dumps({
                            "findings": [{
                                "point": "legacy",
                                "evidence_quote": original_text,
                                "participant_codes": ["P01"],
                                "question_codes": ["Q1"],
                                "confidence": "high",
                            }],
                            "implications": "legacy",
                            "unresolved": "",
                        }, ensure_ascii=False),
                        model_used="test",
                    )
                    db.session.add(legacy)
                    db.session.commit()
                    legacy, unresolved = set_analysis_review_status(legacy, "approved")
                    failures += check(
                        "legacy analysis without generation provenance fails closed",
                        bool(unresolved)
                        and legacy.review_status == "draft"
                        and "provenance" in str(unresolved[0].get("detail") or ""),
                        f"status={legacy.review_status} unresolved={unresolved}",
                    )
                finally:
                    analyzer.call_structured = original_call
                    db.session.rollback()
                    db.session.remove()
                    db.engine.dispose()
        finally:
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
