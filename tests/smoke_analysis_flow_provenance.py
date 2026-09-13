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
        "BACKUP_DIR": config.BACKUP_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_analysis_flow_provenance_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'analysis-flow.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        config.BACKUP_DIR = str(root / "backups")

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
            from models.segment import Segment, UtteranceMapping
            from services.project_flow_scope import (
                ProjectFlowScopeError,
                resolve_integrated_analysis_scope,
            )

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Flow provenance")
                db.session.add(project)
                db.session.flush()

                flow = InterviewFlow(
                    project_id=project.id,
                    title="Guide A",
                    version="2.0",
                )
                db.session.add(flow)
                db.session.flush()

                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
                db.session.add(section)
                db.session.flush()

                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Same question text",
                    seq=1,
                )
                db.session.add(question)
                db.session.flush()

                p1 = Participant(project_id=project.id, participant_code="P01")
                p2 = Participant(project_id=project.id, participant_code="P02")
                db.session.add_all([p1, p2])
                db.session.flush()

                with_evidence = Interview(
                    project_id=project.id,
                    participant_id=p1.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                without_evidence = Interview(
                    project_id=project.id,
                    participant_id=p2.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add_all([with_evidence, without_evidence])
                db.session.flush()

                segment = Segment(
                    interview_id=with_evidence.id,
                    participant_id=p1.id,
                    speaker_label="RESP",
                    speaker_role="respondent",
                    text="mapped answer",
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

                cross = AIAnalysis(
                    project_id=project.id,
                    interview_id=None,
                    question_id=question.id,
                    analysis_type="cross_participant",
                    title="横断分析: [Q1] Same question text",
                    summary_text="",
                    content_json=json.dumps({}, ensure_ascii=False),
                    model_used="stub",
                )
                db.session.add(cross)
                db.session.commit()

                project_id = int(project.id)
                missing_id = int(without_evidence.id)
                cross_id = int(cross.id)
                original_title = str(cross.title)

                try:
                    resolve_integrated_analysis_scope(project)
                    scope_error = None
                except ProjectFlowScopeError as exc:
                    scope_error = exc

                failures += check(
                    "mapped interview with zero prompt evidence blocks integrated scope",
                    scope_error is not None
                    and scope_error.code == "integrated_interview_no_mapped_evidence"
                    and scope_error.interview_ids == [missing_id],
                    f"error={getattr(scope_error, 'code', None)} ids={getattr(scope_error, 'interview_ids', None)}",
                )

            analysis_page = client.get(f"/projects/{project_id}/analysis")
            review_page = client.get(f"/projects/{project_id}/analysis/review")
            expected_label = "Guide A v2.0".encode("utf-8")
            failures += check(
                "cross-analysis result renders flow identity on analysis page",
                analysis_page.status_code == 200 and expected_label in analysis_page.data,
                f"status={analysis_page.status_code}",
            )
            failures += check(
                "cross-analysis result renders flow identity on review page",
                review_page.status_code == 200 and expected_label in review_page.data,
                f"status={review_page.status_code}",
            )

            with app.app_context():
                persisted_title = db.session.get(AIAnalysis, cross_id).title
                failures += check(
                    "render-only flow label does not persistently rewrite analysis title",
                    persisted_title == original_title,
                    f"persisted={persisted_title!r} original={original_title!r}",
                )
                db.session.remove()
                db.engine.dispose()

        except Exception as exc:
            failures += check(
                "analysis flow provenance smoke",
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
            config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
