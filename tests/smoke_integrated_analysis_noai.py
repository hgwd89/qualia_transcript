import json
import subprocess
import sys
import tempfile
from pathlib import Path


TARGET_INTERVIEW_ID = 10


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def repo_state(repo_root: Path) -> dict[str, bool]:
    return {
        "instance": (repo_root / "instance").exists(),
        "uploads": (repo_root / "uploads").exists(),
        "outputs": (repo_root / "outputs").exists(),
    }


def create_fixture() -> dict[str, object]:
    from models import db
    from models.analysis import AIAnalysis
    from models.interview import Interview
    from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
    from models.participant import Participant
    from models.project import Project
    from models.segment import Segment, UtteranceMapping
    from models.segment_flag import SegmentFlag
    from models.speaker_assignment import SpeakerAssignment

    project = Project(name="Integrated No-AI Smoke", status="draft")
    db.session.add(project)
    db.session.flush()

    participant = Participant(
        project_id=project.id,
        participant_code="P01",
        display_name="Smoke Participant",
    )
    db.session.add(participant)
    db.session.flush()

    flow = InterviewFlow(project_id=project.id, title="Smoke Flow")
    db.session.add(flow)
    db.session.flush()
    section = InterviewFlowSection(flow_id=flow.id, title="Main", seq=1)
    db.session.add(section)
    db.session.flush()
    question = InterviewFlowQuestion(
        section_id=section.id,
        question_code="Q1",
        question_text="Integrated smoke question",
        seq=1,
    )
    db.session.add(question)
    db.session.flush()

    interview = Interview(
        id=TARGET_INTERVIEW_ID,
        project_id=project.id,
        participant_id=participant.id,
        flow_id=flow.id,
        status="analyzed",
    )
    db.session.add(interview)
    db.session.flush()

    segments = {
        "respondent": Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="SMOKE_RESPONDENT",
            speaker_role="respondent",
            start_sec=1.0,
            end_sec=4.0,
            text="Respondent evidence from Segment.text.",
            seq=1,
        ),
        "quote": Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="SMOKE_RESPONDENT",
            speaker_role="respondent",
            start_sec=5.0,
            end_sec=8.0,
            text="Quote flag evidence from Segment.text.",
            seq=2,
        ),
        "exclude": Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="SMOKE_RESPONDENT",
            speaker_role="respondent",
            start_sec=9.0,
            end_sec=11.0,
            text="Excluded Segment.text must not be used.",
            seq=3,
        ),
        "needs_review": Segment(
            interview_id=interview.id,
            participant_id=participant.id,
            speaker_label="SMOKE_RESPONDENT",
            speaker_role="respondent",
            start_sec=12.0,
            end_sec=14.0,
            text="Needs review Segment.text is excluded by default.",
            seq=4,
        ),
        "moderator": Segment(
            interview_id=interview.id,
            speaker_label="SMOKE_MODERATOR",
            speaker_role="interviewer",
            start_sec=15.0,
            end_sec=17.0,
            text="Moderator Segment.text must not be evidence.",
            seq=5,
        ),
        "observer": Segment(
            interview_id=interview.id,
            speaker_label="SMOKE_OBSERVER",
            speaker_role="unknown",
            start_sec=18.0,
            end_sec=20.0,
            text="Observer Segment.text must not be evidence.",
            seq=6,
        ),
    }
    db.session.add_all(segments.values())
    db.session.flush()

    db.session.add_all([
        SpeakerAssignment(interview_id=interview.id, speaker_label="SMOKE_RESPONDENT", speaker_role="respondent", participant_id=participant.id),
        SpeakerAssignment(interview_id=interview.id, speaker_label="SMOKE_MODERATOR", speaker_role="moderator"),
        SpeakerAssignment(interview_id=interview.id, speaker_label="SMOKE_OBSERVER", speaker_role="observer"),
        SegmentFlag(segment_id=segments["quote"].id, flag_type="quote"),
        SegmentFlag(segment_id=segments["exclude"].id, flag_type="exclude"),
        SegmentFlag(segment_id=segments["needs_review"].id, flag_type="needs_review"),
        UtteranceMapping(segment_id=segments["respondent"].id, question_id=question.id, mapped_by="manual", confidence=1.0, is_unclassified=False),
        UtteranceMapping(segment_id=segments["quote"].id, question_id=question.id, mapped_by="manual", confidence=1.0, is_unclassified=False),
    ])

    semantic_payload = {
        "cluster_summaries": [
            {
                "cluster_id": "C1",
                "theme": "Smoke theme",
                "summary": "Smoke semantic summary",
                "evidence_source_segment_ids": [segments["respondent"].id, segments["quote"].id],
                "source_segment_quotes": [segments["respondent"].text, segments["quote"].text],
            }
        ]
    }
    db.session.add(AIAnalysis(
        project_id=project.id,
        interview_id=interview.id,
        analysis_type="semantic_clusters",
        title="Smoke semantic clusters",
        summary_text="Smoke semantic summary",
        content_json=json.dumps(semantic_payload, ensure_ascii=False),
        model_used="none",
    ))
    db.session.commit()
    return {name: segment.id for name, segment in segments.items()}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    try:
        import config
        from scripts.run_integrated_analysis import create_analysis_app
        from models import db
        from models.analysis import AIAnalysis
        from models.interview import Interview
        from models.segment import Segment
        from services.integrated_analysis import run_integrated_interview_analysis
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    before_repo_state = repo_state(repo_root)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(tmp_dir / 'integrated_noai_smoke.db').as_posix()}"
        config.UPLOAD_DIR = str(tmp_dir / "uploads")
        config.OUTPUT_DIR = str(tmp_dir / "outputs")

        app = create_analysis_app()
        try:
            with app.app_context():
                db.create_all()
                fixture_ids = create_fixture()

                interview = db.session.get(Interview, TARGET_INTERVIEW_ID)
                failures += 0 if print_result(
                    "target interview exists",
                    interview is not None,
                    f"interview_id={TARGET_INTERVIEW_ID}",
                ) else 1
                if interview is None:
                    print("\nSummary: FAIL")
                    return 1

                before_analysis_total = AIAnalysis.query.count()
                before_analysis_interview = AIAnalysis.query.filter_by(interview_id=TARGET_INTERVIEW_ID).count()

                segments = (
                    Segment.query
                    .filter_by(interview_id=TARGET_INTERVIEW_ID)
                    .order_by(Segment.seq.asc())
                    .all()
                )
                before_text_map = {s.id: s.text for s in segments}
                segment_by_id = {s.id: s for s in segments}

                result = run_integrated_interview_analysis(
                    interview_id=TARGET_INTERVIEW_ID,
                    no_ai=True,
                    save=False,
                    max_quotes=20,
                    include_needs_review=False,
                )
                payload = result.get("payload") or {}

                failures += 0 if print_result("result ok", bool(result.get("ok"))) else 1
                failures += 0 if print_result(
                    "analysis_type",
                    payload.get("analysis_type") == "integrated_interview_analysis",
                    f"value={payload.get('analysis_type')}",
                ) else 1
                failures += 0 if print_result(
                    "mode no_ai_dry_run",
                    payload.get("mode") == "no_ai_dry_run",
                    f"value={payload.get('mode')}",
                ) else 1

                required_keys = {
                    "analysis_type",
                    "interview_id",
                    "mode",
                    "key_findings",
                    "supporting_quotes",
                    "participant_insights",
                    "question_insights",
                    "semantic_cluster_insights",
                    "product_or_brand_mentions",
                    "implications",
                    "unresolved_questions",
                    "cautions",
                    "evidence_map",
                    "source_segment_ids",
                    "source_segment_quotes",
                    "flag_summary",
                    "speaker_assignment_summary",
                    "models",
                    "created_at",
                }
                missing = sorted(list(required_keys - set(payload.keys())))
                failures += 0 if print_result(
                    "payload keys",
                    len(missing) == 0,
                    f"missing={missing}",
                ) else 1

                source_ids = payload.get("source_segment_ids") or []
                source_quotes = payload.get("source_segment_quotes") or []
                exact_match_count = 0
                for sid, quote in zip(source_ids, source_quotes):
                    seg = segment_by_id.get(int(sid))
                    if seg and seg.text == quote:
                        exact_match_count += 1
                failures += 0 if print_result(
                    "source quotes exact match",
                    exact_match_count == len(source_quotes),
                    f"exact={exact_match_count}/{len(source_quotes)}",
                ) else 1

                support_segment_ids = {
                    int(q.get("segment_id"))
                    for q in (payload.get("supporting_quotes") or [])
                    if q.get("segment_id") is not None
                }
                source_ids_set = {int(x) for x in source_ids if str(x).isdigit()}
                blocked_ids = {
                    fixture_ids["exclude"],
                    fixture_ids["needs_review"],
                    fixture_ids["moderator"],
                    fixture_ids["observer"],
                }
                blocked_included = (blocked_ids & support_segment_ids) or (blocked_ids & source_ids_set)
                failures += 0 if print_result(
                    "exclude/needs_review/moderator/observer not included",
                    len(blocked_included) == 0,
                    f"found={sorted(list(blocked_included))}",
                ) else 1

                supporting_quotes = payload.get("supporting_quotes") or []
                first_segment_id = supporting_quotes[0].get("segment_id") if supporting_quotes else None
                failures += 0 if print_result(
                    "quote flag prioritized",
                    first_segment_id == fixture_ids["quote"],
                    f"first_segment_id={first_segment_id}, quote_id={fixture_ids['quote']}",
                ) else 1

                speaker_summary = payload.get("speaker_assignment_summary") or {}
                failures += 0 if print_result(
                    "speaker_assignment_summary exists",
                    isinstance(speaker_summary, dict) and "unresolved_labels" in speaker_summary,
                    str(speaker_summary),
                ) else 1

                failures += 0 if print_result(
                    "api call count is zero",
                    result.get("api_call_count") == 0,
                    f"value={result.get('api_call_count')}",
                ) else 1
                failures += 0 if print_result(
                    "db update not performed",
                    result.get("db_update_performed") is False,
                    f"value={result.get('db_update_performed')}",
                ) else 1

                after_analysis_total = AIAnalysis.query.count()
                after_analysis_interview = AIAnalysis.query.filter_by(interview_id=TARGET_INTERVIEW_ID).count()
                failures += 0 if print_result(
                    "AIAnalysis count unchanged",
                    after_analysis_total == before_analysis_total and after_analysis_interview == before_analysis_interview,
                    f"before_total={before_analysis_total}, after_total={after_analysis_total}, before_iv={before_analysis_interview}, after_iv={after_analysis_interview}",
                ) else 1

                segments_after = Segment.query.filter_by(interview_id=TARGET_INTERVIEW_ID).all()
                unchanged = all(before_text_map.get(s.id) == s.text for s in segments_after)
                failures += 0 if print_result("segment text unchanged", unchanged) else 1

                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    after_repo_state = repo_state(repo_root)
    failures += 0 if print_result(
        "repo instance/uploads/outputs state unchanged",
        before_repo_state == after_repo_state,
        f"before={before_repo_state}, after={after_repo_state}",
    ) else 1

    status_proc = run_git(repo_root, "status", "--short")
    if status_proc.returncode == 0:
        out = status_proc.stdout.strip()
        print("git status --short:")
        print(out if out else "(clean)")
    else:
        failures += 0 if print_result(
            "git status --short",
            False,
            (status_proc.stderr or status_proc.stdout or "unknown git error").strip(),
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
