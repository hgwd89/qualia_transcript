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

    with tempfile.TemporaryDirectory(prefix="qualia_preview_fgi_attribution_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'preview-fgi.db').as_posix()}"
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
            from services.integrated_analysis import run_integrated_interview_analysis

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                fgi = Project(name="FGI preview", method="FGI")
                db.session.add(fgi)
                db.session.flush()
                p01 = Participant(project_id=fgi.id, participant_code="P01", display_name="Owner")
                p02 = Participant(project_id=fgi.id, participant_code="P02", display_name="Participant 2")
                db.session.add_all([p01, p02])
                db.session.flush()
                interview = Interview(
                    project_id=fgi.id,
                    participant_id=p01.id,
                    status="transcribed",
                )
                db.session.add(interview)
                db.session.flush()
                unassigned = Segment(
                    interview_id=interview.id,
                    participant_id=None,
                    speaker_label="SPEAKER_UNASSIGNED",
                    speaker_role="respondent",
                    text="FGI respondent with unresolved participant.",
                    seq=1,
                )
                assigned = Segment(
                    interview_id=interview.id,
                    participant_id=p02.id,
                    speaker_label="SPEAKER_P02",
                    speaker_role="respondent",
                    text="FGI respondent explicitly assigned to P02.",
                    seq=2,
                )
                db.session.add_all([unassigned, assigned])
                db.session.commit()

                result = run_integrated_interview_analysis(
                    interview_id=int(interview.id),
                    no_ai=True,
                    save=False,
                )
                payload = result.get("payload") or {}
                quotes = payload.get("supporting_quotes") or []
                quote_by_segment = {
                    int(row["segment_id"]): row
                    for row in quotes
                    if row.get("segment_id") is not None
                }
                failures += check(
                    "FGI unassigned respondent does not fall back to Interview.participant",
                    int(unassigned.id) in quote_by_segment
                    and quote_by_segment[int(unassigned.id)].get("participant_code") is None,
                    f"quote={quote_by_segment.get(int(unassigned.id))}",
                )
                failures += check(
                    "FGI explicitly assigned respondent keeps canonical participant",
                    int(assigned.id) in quote_by_segment
                    and quote_by_segment[int(assigned.id)].get("participant_code") == "P02",
                    f"quote={quote_by_segment.get(int(assigned.id))}",
                )
                participant_codes = {
                    str(row.get("participant_code") or "")
                    for row in (payload.get("participant_insights") or [])
                }
                failures += check(
                    "FGI participant buckets do not invent owner attribution",
                    participant_codes == {"P02"},
                    f"participant_codes={sorted(participant_codes)}",
                )
                cautions = [str(value) for value in (payload.get("cautions") or [])]
                failures += check(
                    "FGI unassigned respondent is surfaced as caution",
                    any(
                        str(unassigned.id) in value
                        and "participant" in value
                        and "FGI" in value
                        for value in cautions
                    ),
                    f"cautions={cautions}",
                )

                di = Project(name="DI preview", method="DI")
                db.session.add(di)
                db.session.flush()
                di_owner = Participant(project_id=di.id, participant_code="D01", display_name="DI Owner")
                db.session.add(di_owner)
                db.session.flush()
                di_interview = Interview(
                    project_id=di.id,
                    participant_id=di_owner.id,
                    status="transcribed",
                )
                db.session.add(di_interview)
                db.session.flush()
                di_segment = Segment(
                    interview_id=di_interview.id,
                    participant_id=None,
                    speaker_label="SPEAKER_DI",
                    speaker_role="respondent",
                    text="DI segment may inherit the interview participant.",
                    seq=1,
                )
                db.session.add(di_segment)
                db.session.commit()

                di_result = run_integrated_interview_analysis(
                    interview_id=int(di_interview.id),
                    no_ai=True,
                    save=False,
                )
                di_quotes = (di_result.get("payload") or {}).get("supporting_quotes") or []
                failures += check(
                    "DI keeps interview-participant fallback",
                    len(di_quotes) == 1 and di_quotes[0].get("participant_code") == "D01",
                    f"quotes={di_quotes}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "integrated preview FGI attribution smoke",
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
