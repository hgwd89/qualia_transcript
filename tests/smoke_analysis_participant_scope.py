from __future__ import annotations

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
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_analysis_participant_scope_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'participant-scope.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment
            import services.analyzer as analyzer
            from services.analysis_source_provenance import (
                AnalysisSourceProvenanceError,
                analysis_source_provenance_status,
                capture_analysis_source_provenance,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                fgi_project = Project(name="FGI scope", method="FGI")
                db.session.add(fgi_project)
                db.session.flush()
                fgi_flow = InterviewFlow(project_id=fgi_project.id, title="FGI flow", version="1.0")
                db.session.add(fgi_flow)
                db.session.flush()
                fgi_section = InterviewFlowSection(flow_id=fgi_flow.id, title="Section", seq=1)
                db.session.add(fgi_section)
                db.session.flush()
                fgi_question = InterviewFlowQuestion(
                    section_id=fgi_section.id,
                    question_code="Q1",
                    question_text="FGI question",
                    seq=1,
                )
                db.session.add(fgi_question)
                fgi_participant = Participant(
                    project_id=fgi_project.id,
                    participant_code="P01",
                    display_name="FGI participant",
                )
                db.session.add(fgi_participant)
                db.session.flush()
                fgi_interview = Interview(
                    project_id=fgi_project.id,
                    participant_id=fgi_participant.id,
                    flow_id=fgi_flow.id,
                    status="mapped",
                )
                db.session.add(fgi_interview)
                db.session.flush()
                db.session.add(Segment(
                    interview_id=fgi_interview.id,
                    participant_id=fgi_participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="FGI respondent statement with enough content",
                    seq=1,
                ))

                di_project = Project(name="DI mixed scope", method="DI")
                db.session.add(di_project)
                db.session.flush()
                di_owner = Participant(
                    project_id=di_project.id,
                    participant_code="P01",
                    display_name="Interview owner",
                )
                di_other = Participant(
                    project_id=di_project.id,
                    participant_code="P02",
                    display_name="Other respondent",
                )
                db.session.add_all([di_owner, di_other])
                db.session.flush()
                di_mixed_interview = Interview(
                    project_id=di_project.id,
                    participant_id=di_owner.id,
                    status="transcribed",
                )
                db.session.add(di_mixed_interview)
                db.session.flush()
                mixed_segment = Segment(
                    interview_id=di_mixed_interview.id,
                    participant_id=di_other.id,
                    speaker_label="SPEAKER_02",
                    speaker_role="respondent",
                    text="Respondent explicitly attributed to another participant",
                    seq=1,
                )
                db.session.add(mixed_segment)

                di_clean_interview = Interview(
                    project_id=di_project.id,
                    participant_id=di_owner.id,
                    status="transcribed",
                )
                db.session.add(di_clean_interview)
                db.session.flush()
                db.session.add_all([
                    Segment(
                        interview_id=di_clean_interview.id,
                        participant_id=di_owner.id,
                        speaker_label="SPEAKER_01",
                        speaker_role="respondent",
                        text="Canonical DI respondent statement",
                        seq=1,
                    ),
                    Segment(
                        interview_id=di_clean_interview.id,
                        participant_id=None,
                        speaker_label="SPEAKER_01",
                        speaker_role="respondent",
                        text="Unassigned DI statement falls back to interview participant",
                        seq=2,
                    ),
                ])
                db.session.commit()

                fgi_project_id = int(fgi_project.id)
                fgi_interview_id = int(fgi_interview.id)
                fgi_question_id = int(fgi_question.id)
                di_project_id = int(di_project.id)
                di_mixed_interview_id = int(di_mixed_interview.id)
                mixed_segment_id = int(mixed_segment.id)
                di_clean_interview_id = int(di_clean_interview.id)

                for analysis_type, kwargs in (
                    ("per_question", {"interview_id": fgi_interview_id, "question_id": fgi_question_id}),
                    ("per_participant", {"interview_id": fgi_interview_id}),
                    ("cross_participant", {"question_id": fgi_question_id}),
                    ("integrated", {}),
                ):
                    rejected = False
                    detail = ""
                    try:
                        capture_analysis_source_provenance(
                            analysis_type,
                            fgi_project_id,
                            **kwargs,
                        )
                    except AnalysisSourceProvenanceError as exc:
                        detail = str(exc)
                        rejected = "FGI" in detail
                    failures += check(
                        f"{analysis_type} provenance rejects FGI participant attribution",
                        rejected,
                        detail,
                    )

                provider_calls = 0
                original_call = analyzer.call_structured

                def forbidden_provider(*_args, **_kwargs):
                    nonlocal provider_calls
                    provider_calls += 1
                    raise AssertionError("provider must not be called for unsupported FGI analysis")

                analyzer.call_structured = forbidden_provider
                provider_error = ""
                try:
                    analyzer.analyze_interview_summary(
                        fgi_interview_id,
                        result_write_guard=lambda: None,
                    )
                except AnalysisSourceProvenanceError as exc:
                    provider_error = str(exc)
                finally:
                    analyzer.call_structured = original_call
                failures += check(
                    "FGI persisted analysis fails before provider work",
                    provider_calls == 0 and "FGI" in provider_error,
                    f"provider_calls={provider_calls} error={provider_error}",
                )

                mixed_rejected = False
                mixed_reason = ""
                try:
                    capture_analysis_source_provenance(
                        "per_participant",
                        di_project_id,
                        interview_id=di_mixed_interview_id,
                    )
                except AnalysisSourceProvenanceError as exc:
                    mixed_reason = str(exc)
                    mixed_rejected = (
                        str(mixed_segment_id) in mixed_reason
                        and "differs from Interview.participant" in mixed_reason
                    )
                failures += check(
                    "DI analysis rejects respondent segment attributed to another participant",
                    mixed_rejected,
                    mixed_reason,
                )

                clean_provenance = capture_analysis_source_provenance(
                    "per_participant",
                    di_project_id,
                    interview_id=di_clean_interview_id,
                )
                failures += check(
                    "ordinary DI analysis remains provenance-supported",
                    clean_provenance.get("version") == "analysis-input-v1"
                    and len(str(clean_provenance.get("sha256") or "")) == 64,
                    str(clean_provenance),
                )

                historical_fgi = AIAnalysis(
                    project_id=fgi_project_id,
                    interview_id=fgi_interview_id,
                    analysis_type="per_participant",
                    title="Historical FGI analysis",
                    summary_text="historical",
                    content_json=json.dumps({
                        "findings": [],
                        "source_provenance": {
                            "version": "analysis-input-v1",
                            "sha256": "0" * 64,
                        },
                    }),
                    model_used="test",
                )
                db.session.add(historical_fgi)
                db.session.commit()
                current, reason = analysis_source_provenance_status(historical_fgi)
                failures += check(
                    "historical FGI analysis cannot be treated as current formal input",
                    not current and "FGI" in reason,
                    reason,
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "analysis participant scope smoke",
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
