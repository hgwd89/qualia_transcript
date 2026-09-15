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
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_integrated_preview_currentness_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'preview-currentness.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        app = None
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
            from services.integrated_analysis import run_integrated_interview_analysis
            from services.semantic_source_provenance import capture_semantic_source_provenance

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Derived currentness", method="DI")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="What matters?",
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant",
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
                    text="Canonical current evidence.",
                    seq=1,
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
                segment_id = int(segment.id)

                semantic_provenance = capture_semantic_source_provenance(
                    project_id,
                    interview_id,
                    max_segments=None,
                    no_ai=True,
                )
                current_semantic = AIAnalysis(
                    project_id=project_id,
                    interview_id=interview_id,
                    analysis_type="semantic_clusters",
                    title="Current semantic",
                    summary_text="CURRENT SEMANTIC",
                    content_json=json.dumps({
                        "semantic_request": {"max_segments": None, "no_ai": True},
                        "source_provenance": semantic_provenance,
                        "cluster_summaries": [{
                            "cluster_id": "C-current",
                            "theme": "Current theme",
                            "summary": "CURRENT SEMANTIC",
                            "evidence_source_segment_ids": [segment_id],
                        }],
                    }),
                    model_used="test-current-semantic",
                )
                db.session.add(current_semantic)
                db.session.flush()

                current_question_provenance = capture_analysis_source_provenance(
                    "per_question",
                    project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                )
                current_question = AIAnalysis(
                    project_id=project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                    analysis_type="per_question",
                    title="Current question",
                    summary_text="CURRENT QUESTION",
                    content_json=json.dumps({
                        "question_id": question_id,
                        "question_code": "Q1",
                        "question_text": "What matters?",
                        "findings": [],
                        "implications": "CURRENT QUESTION",
                        "unresolved": "",
                        "source_provenance": current_question_provenance,
                    }),
                    model_used="test-current-question",
                )
                db.session.add(current_question)
                db.session.flush()

                # Newer rows are deliberately stale/unprovable. Selection must
                # skip them and fall back to the older current rows above.
                db.session.add(AIAnalysis(
                    project_id=project_id,
                    interview_id=interview_id,
                    analysis_type="semantic_clusters",
                    title="Stale semantic",
                    summary_text="STALE SEMANTIC",
                    content_json=json.dumps({
                        "cluster_summaries": [{
                            "cluster_id": "C-stale",
                            "theme": "Stale theme",
                            "summary": "STALE SEMANTIC",
                            "evidence_source_segment_ids": [segment_id],
                        }],
                    }),
                    model_used="test-stale-semantic",
                ))
                db.session.add(AIAnalysis(
                    project_id=project_id,
                    interview_id=interview_id,
                    question_id=question_id,
                    analysis_type="per_question",
                    title="Stale question",
                    summary_text="STALE QUESTION",
                    content_json=json.dumps({
                        "question_id": question_id,
                        "question_code": "Q1",
                        "question_text": "What matters?",
                        "findings": [],
                        "implications": "STALE QUESTION",
                        "unresolved": "",
                    }),
                    model_used="test-stale-question",
                ))
                db.session.commit()

                result = run_integrated_interview_analysis(
                    interview_id=interview_id,
                    no_ai=True,
                    save=False,
                )
                payload = result.get("payload") or {}
                semantic_summaries = [
                    str(row.get("summary") or "")
                    for row in (payload.get("semantic_cluster_insights") or [])
                ]
                question_summaries = [
                    str(row.get("summary") or "")
                    for row in (payload.get("question_insights") or [])
                ]
                cautions = [str(value) for value in (payload.get("cautions") or [])]

                failures += check(
                    "newer stale semantic row is skipped in favor of current row",
                    semantic_summaries == ["CURRENT SEMANTIC"]
                    and "STALE SEMANTIC" not in semantic_summaries,
                    f"semantic={semantic_summaries}",
                )
                failures += check(
                    "newer stale per-question row is skipped in favor of current row",
                    question_summaries == ["CURRENT QUESTION"]
                    and "STALE QUESTION" not in question_summaries,
                    f"question={question_summaries}",
                )
                failures += check(
                    "preview reports skipped stale derived rows",
                    any("semantic" in value and "skipped" in value for value in cautions)
                    and any("per_question" in value and "skipped" in value for value in cautions),
                    f"cautions={cautions}",
                )

                # Canonical input drift makes every previously generated derived
                # analysis stale. The preview must keep the live Segment quote but
                # drop all stale semantic/question summaries.
                segment = db.session.get(Segment, segment_id)
                segment.text = "Canonical evidence changed after analyses."
                db.session.commit()

                drifted = run_integrated_interview_analysis(
                    interview_id=interview_id,
                    no_ai=True,
                    save=False,
                )
                drifted_payload = drifted.get("payload") or {}
                drifted_quotes = drifted_payload.get("supporting_quotes") or []
                failures += check(
                    "canonical drift removes all stale semantic insights",
                    (drifted_payload.get("semantic_cluster_insights") or []) == [],
                    f"semantic={drifted_payload.get('semantic_cluster_insights')}",
                )
                failures += check(
                    "canonical drift removes all stale per-question insights",
                    (drifted_payload.get("question_insights") or []) == [],
                    f"question={drifted_payload.get('question_insights')}",
                )
                failures += check(
                    "canonical live quote remains visible after derived rows are dropped",
                    len(drifted_quotes) == 1
                    and drifted_quotes[0].get("text") == "Canonical evidence changed after analyses.",
                    f"quotes={drifted_quotes}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "integrated preview derived currentness smoke",
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
