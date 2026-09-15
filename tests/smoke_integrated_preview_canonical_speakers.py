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

    with tempfile.TemporaryDirectory(prefix="qualia_integrated_preview_canonical_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'preview.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        app = None
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
                project = Project(name="Canonical preview", method="DI")
                db.session.add(project)
                db.session.flush()

                owner = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Canonical Owner",
                )
                stale_other = Participant(
                    project_id=project.id,
                    participant_code="P02",
                    display_name="Stale Assignment Participant",
                )
                db.session.add_all([owner, stale_other])
                db.session.flush()

                interview = Interview(
                    project_id=project.id,
                    participant_id=owner.id,
                    status="transcribed",
                )
                db.session.add(interview)
                db.session.flush()

                respondent = Segment(
                    interview_id=interview.id,
                    participant_id=owner.id,
                    speaker_label="SPEAKER_RESPONDENT",
                    speaker_role="respondent",
                    text="Canonical respondent evidence.",
                    seq=1,
                )
                legacy_moderator = Segment(
                    interview_id=interview.id,
                    participant_id=None,
                    speaker_label="SPEAKER_MODERATOR",
                    speaker_role="interviewer",
                    text="Legacy moderator must not become evidence.",
                    seq=2,
                )
                unknown = Segment(
                    interview_id=interview.id,
                    participant_id=None,
                    speaker_label="SPEAKER_UNKNOWN",
                    speaker_role="unknown",
                    text="Unknown speaker must not become evidence.",
                    seq=3,
                )
                observer = Segment(
                    interview_id=interview.id,
                    participant_id=None,
                    speaker_label="SPEAKER_OBSERVER",
                    speaker_role="observer",
                    text="Observer must not become evidence.",
                    seq=4,
                )
                db.session.add_all([respondent, legacy_moderator, unknown, observer])
                db.session.flush()

                # Deliberately stale/contradictory UI-assignment rows. Since #146,
                # Segment is the canonical analysis input; these rows are review
                # metadata only and must never override canonical role/participant.
                db.session.add_all([
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="SPEAKER_RESPONDENT",
                        speaker_role="moderator",
                        participant_id=stale_other.id,
                    ),
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="SPEAKER_MODERATOR",
                        speaker_role="respondent",
                        participant_id=stale_other.id,
                    ),
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="SPEAKER_UNKNOWN",
                        speaker_role="respondent",
                        participant_id=stale_other.id,
                    ),
                    SpeakerAssignment(
                        interview_id=interview.id,
                        speaker_label="SPEAKER_OBSERVER",
                        speaker_role="respondent",
                        participant_id=stale_other.id,
                    ),
                ])
                db.session.commit()

                result = run_integrated_interview_analysis(
                    interview_id=int(interview.id),
                    no_ai=True,
                    save=False,
                    max_quotes=20,
                )
                payload = result.get("payload") or {}
                quotes = payload.get("supporting_quotes") or []
                quote_segment_ids = {
                    int(row["segment_id"])
                    for row in quotes
                    if row.get("segment_id") is not None
                }

                failures += check(
                    "only canonical respondent becomes evidence",
                    quote_segment_ids == {int(respondent.id)},
                    f"quote_segment_ids={sorted(quote_segment_ids)}",
                )
                failures += check(
                    "canonical participant wins over stale assignment participant",
                    len(quotes) == 1
                    and quotes[0].get("participant_code") == "P01"
                    and quotes[0].get("speaker_role") == "respondent",
                    f"quotes={quotes}",
                )

                participant_insights = payload.get("participant_insights") or []
                failures += check(
                    "participant buckets follow canonical segment participant",
                    len(participant_insights) == 1
                    and participant_insights[0].get("participant_code") == "P01"
                    and int(participant_insights[0].get("segment_count") or 0) == 1,
                    f"participant_insights={participant_insights}",
                )
                failures += check(
                    "legacy interviewer alias and non-respondents are excluded",
                    int(result.get("excluded_segment_count") or 0) == 3,
                    f"excluded={result.get('excluded_segment_count')}",
                )
                failures += check(
                    "dry run remains read-only",
                    result.get("db_update_performed") is False
                    and result.get("api_call_count") == 0,
                    f"db_update={result.get('db_update_performed')} api_calls={result.get('api_call_count')}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "integrated preview canonical speaker smoke",
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
