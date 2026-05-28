import json
import sys
import tempfile
from pathlib import Path

from flask import Flask

TARGET_INTERVIEW_ID = 10


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def repo_state(repo_root: Path) -> dict[str, bool]:
    return {
        "instance": (repo_root / "instance").exists(),
        "uploads": (repo_root / "uploads").exists(),
        "outputs": (repo_root / "outputs").exists(),
    }


def import_models():
    # Import model classes explicitly so SQLAlchemy relationship strings resolve
    # without importing app.py or any transcription/AI execution path.
    from models.project import Project  # noqa: F401
    from models.participant import Participant, ParticipantAttribute  # noqa: F401
    from models.interview_flow import InterviewFlow, InterviewFlowSection, InterviewFlowQuestion  # noqa: F401
    from models.interview import Interview, MediaFile, Transcription  # noqa: F401
    from models.segment import Segment, UtteranceMapping  # noqa: F401
    from models.segment_flag import SegmentFlag  # noqa: F401
    from models.speaker_assignment import SpeakerAssignment  # noqa: F401
    from models.analysis import AIAnalysis  # noqa: F401
    from models.generated_file import GeneratedFile  # noqa: F401
    from models.setting import AppSetting  # noqa: F401


def create_preview_app(database_uri: str, upload_dir: Path, output_dir: Path) -> Flask:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    from models import db

    config.DATABASE_URI = database_uri
    config.UPLOAD_DIR = str(upload_dir)
    config.OUTPUT_DIR = str(output_dir)

    import_models()

    from routes.projects import bp as projects_bp
    from routes.interviews import bp as interviews_bp
    from routes.settings import bp as settings_bp

    app = Flask(__name__, template_folder=str(repo_root / "templates"))
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(projects_bp)
    app.register_blueprint(interviews_bp)
    app.register_blueprint(settings_bp)
    return app


def create_fixture() -> dict[str, str]:
    from models import db
    from models.analysis import AIAnalysis
    from models.interview import Interview
    from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
    from models.participant import Participant
    from models.project import Project
    from models.segment import Segment, UtteranceMapping
    from models.segment_flag import SegmentFlag
    from models.speaker_assignment import SpeakerAssignment

    project = Project(name="Integrated Preview Smoke", status="draft")
    db.session.add(project)
    db.session.flush()

    participant = Participant(
        project_id=project.id,
        participant_code="P01",
        display_name="Preview Participant",
    )
    db.session.add(participant)
    db.session.flush()

    flow = InterviewFlow(project_id=project.id, title="Preview Flow")
    db.session.add(flow)
    db.session.flush()
    section = InterviewFlowSection(flow_id=flow.id, title="Main", seq=1)
    db.session.add(section)
    db.session.flush()
    question = InterviewFlowQuestion(
        section_id=section.id,
        question_code="Q1",
        question_text="Preview question",
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

    quote_text = "Preview quote text from Segment.text"
    normal_text = "Preview supporting respondent text"
    excluded_text = "Preview excluded text must not be used"
    moderator_text = "Preview moderator text must not be evidence"
    segments = [
        Segment(interview_id=interview.id, participant_id=participant.id, speaker_label="SPK_P01", speaker_role="respondent", start_sec=1.0, end_sec=4.0, text=quote_text, seq=1),
        Segment(interview_id=interview.id, participant_id=participant.id, speaker_label="SPK_P01", speaker_role="respondent", start_sec=5.0, end_sec=8.0, text=normal_text, seq=2),
        Segment(interview_id=interview.id, participant_id=participant.id, speaker_label="SPK_P01", speaker_role="respondent", start_sec=9.0, end_sec=12.0, text=excluded_text, seq=3),
        Segment(interview_id=interview.id, speaker_label="SPK_MOD", speaker_role="interviewer", start_sec=13.0, end_sec=15.0, text=moderator_text, seq=4),
    ]
    db.session.add_all(segments)
    db.session.flush()

    db.session.add_all([
        SpeakerAssignment(interview_id=interview.id, speaker_label="SPK_P01", speaker_role="respondent", participant_id=participant.id),
        SpeakerAssignment(interview_id=interview.id, speaker_label="SPK_MOD", speaker_role="moderator"),
        SegmentFlag(segment_id=segments[0].id, flag_type="quote"),
        SegmentFlag(segment_id=segments[2].id, flag_type="exclude"),
        UtteranceMapping(segment_id=segments[0].id, question_id=question.id, mapped_by="manual", confidence=1.0, is_unclassified=False),
        UtteranceMapping(segment_id=segments[1].id, question_id=question.id, mapped_by="manual", confidence=1.0, is_unclassified=False),
    ])
    semantic_payload = {
        "cluster_summaries": [
            {
                "cluster_id": "C1",
                "theme": "Preview theme",
                "summary": "Preview semantic cluster summary",
                "evidence_source_segment_ids": [segments[0].id, segments[1].id],
                "source_segment_quotes": [segments[0].text, segments[1].text],
            }
        ]
    }
    db.session.add(AIAnalysis(
        project_id=project.id,
        interview_id=interview.id,
        analysis_type="semantic_clusters",
        title="Preview semantic clusters",
        summary_text="Preview semantic summary",
        content_json=json.dumps(semantic_payload, ensure_ascii=False),
        model_used="none",
    ))
    db.session.commit()
    return {"quote_text": quote_text, "normal_text": normal_text}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    before_repo_state = repo_state(repo_root)
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        database_uri = f"sqlite:///{(tmp_dir / 'preview_ui_smoke.db').as_posix()}"
        app = create_preview_app(
            database_uri=database_uri,
            upload_dir=tmp_dir / "uploads",
            output_dir=tmp_dir / "outputs",
        )

        from models import db
        from models.analysis import AIAnalysis
        from models.interview import Interview
        from models.segment import Segment

        try:
            with app.app_context():
                db.create_all()
                fixture = create_fixture()

                interview = db.session.get(Interview, TARGET_INTERVIEW_ID)
                failures += 0 if print_result(
                    "target interview exists",
                    interview is not None,
                    f"interview_id={TARGET_INTERVIEW_ID}",
                ) else 1

                before_analysis_count = AIAnalysis.query.count()
                segments = (
                    Segment.query
                    .filter_by(interview_id=TARGET_INTERVIEW_ID)
                    .order_by(Segment.seq.asc())
                    .all()
                )
                before_text_map = {seg.id: seg.text for seg in segments}

                client = app.test_client()
                response = client.get(f"/interviews/{TARGET_INTERVIEW_ID}/integrated-analysis/dry-run")
                html = response.get_data(as_text=True)

                failures += 0 if print_result(
                    "preview route returns 200",
                    response.status_code == 200,
                    f"status={response.status_code}",
                ) else 1
                failures += 0 if print_result(
                    "preview test id rendered",
                    'data-testid="integrated-analysis-preview"' in html,
                ) else 1
                failures += 0 if print_result(
                    "api call count shown as zero",
                    'data-testid="api-call-count"' in html and ">0<" in html,
                ) else 1
                failures += 0 if print_result(
                    "supporting quotes section rendered",
                    "根拠発話 supporting_quotes" in html,
                ) else 1
                failures += 0 if print_result(
                    "evidence_map section rendered",
                    "evidence_map" in html,
                ) else 1
                failures += 0 if print_result(
                    "supporting quote text rendered",
                    fixture["quote_text"] in html,
                ) else 1
                failures += 0 if print_result(
                    "participant insights render bucket fields",
                    "segment_count=" in html and "quote_segment_count=" in html and "speaker_labels:" in html,
                ) else 1

                after_analysis_count = AIAnalysis.query.count()
                after_segments = Segment.query.filter(Segment.id.in_(before_text_map.keys())).all()
                after_text_map = {seg.id: seg.text for seg in after_segments}
                failures += 0 if print_result(
                    "AIAnalysis count unchanged",
                    before_analysis_count == after_analysis_count,
                    f"before={before_analysis_count}, after={after_analysis_count}",
                ) else 1
                failures += 0 if print_result(
                    "Segment.text unchanged",
                    before_text_map == after_text_map,
                ) else 1

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

    if failures:
        print("\nSummary: FAIL")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
