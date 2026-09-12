import re
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

    with tempfile.TemporaryDirectory(prefix="qualia_integrated_evidence_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'preview.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment
            from models.speaker_assignment import SpeakerAssignment
            from services.integrated_analysis import run_integrated_interview_analysis

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Integrated evidence smoke")
                db.session.add(project)
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
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()

                segments = [
                    Segment(
                        interview_id=interview.id,
                        participant_id=participant.id,
                        seq=1,
                        speaker_label="RESPONDENT_DIRECT",
                        speaker_role="respondent",
                        text="direct respondent evidence",
                    ),
                    Segment(
                        interview_id=interview.id,
                        seq=2,
                        speaker_label="LEGACY_INTERVIEWER",
                        speaker_role="interviewer",
                        text="legacy interviewer must be excluded",
                    ),
                    Segment(
                        interview_id=interview.id,
                        seq=3,
                        speaker_label="UNRESOLVED_UNKNOWN",
                        speaker_role="unknown",
                        text="unknown speaker must be excluded",
                    ),
                    Segment(
                        interview_id=interview.id,
                        seq=4,
                        speaker_label="ASSIGNED_RESPONDENT",
                        speaker_role="unknown",
                        text="speaker assignment respondent evidence",
                    ),
                    Segment(
                        interview_id=interview.id,
                        seq=5,
                        speaker_label="ASSIGNED_MODERATOR",
                        speaker_role="unknown",
                        text="speaker assignment moderator must be excluded",
                    ),
                ]
                db.session.add_all(segments)
                db.session.flush()
                db.session.add_all([
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="ASSIGNED_RESPONDENT",
                        speaker_role="respondent",
                        participant_id=participant.id,
                    ),
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="ASSIGNED_MODERATOR",
                        speaker_role="moderator",
                    ),
                ])
                db.session.commit()
                interview_id = int(interview.id)

                result = run_integrated_interview_analysis(
                    interview_id=interview_id,
                    no_ai=True,
                    save=False,
                    max_quotes=20,
                )
                quote_texts = {
                    row.get("text")
                    for row in ((result.get("payload") or {}).get("supporting_quotes") or [])
                }
                failures += check(
                    "integrated evidence keeps only effective respondents",
                    quote_texts == {
                        "direct respondent evidence",
                        "speaker assignment respondent evidence",
                    },
                    str(sorted(quote_texts)),
                )
                failures += check(
                    "non-respondent evidence is counted as excluded",
                    result.get("excluded_segment_count") == 3,
                    str(result.get("excluded_segment_count")),
                )

            client = app.test_client()
            response = client.get(
                f"/interviews/{interview_id}/integrated-analysis/dry-run"
            )
            html = response.get_data(as_text=True)
            api_count_match = re.search(
                r'data-testid="api-call-count"[^>]*>\s*([^<]+?)\s*</div>',
                html,
                flags=re.IGNORECASE,
            )
            failures += check(
                "preview API count targeted element is exactly zero",
                response.status_code == 200
                and api_count_match is not None
                and api_count_match.group(1).strip() == "0",
                api_count_match.group(1).strip() if api_count_match else "missing",
            )
            failures += check(
                "preview does not render interviewer or unknown evidence",
                "legacy interviewer must be excluded" not in html
                and "unknown speaker must be excluded" not in html
                and "speaker assignment moderator must be excluded" not in html,
            )
            failures += check(
                "preview renders effective respondent evidence",
                "direct respondent evidence" in html
                and "speaker assignment respondent evidence" in html,
            )
        except Exception as exc:
            failures += check(
                "integrated preview evidence safety smoke",
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
