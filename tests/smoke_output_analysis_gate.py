import json
import subprocess
import sys
from pathlib import Path

from flask import Flask


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
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    try:
        from models import db
        from models.analysis import AIAnalysis
        from models.generated_file import GeneratedFile  # noqa: F401 (mapper registry)
        from models.interview import Interview, MediaFile, Transcription  # noqa: F401
        from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection  # noqa: F401
        from models.participant import Participant, ParticipantAttribute  # noqa: F401
        from models.project import Project
        from models.quote_candidate import QuoteCandidate  # noqa: F401
        from models.review_item import ReviewItem  # noqa: F401
        from models.segment import Segment, UtteranceMapping  # noqa: F401
        from models.segment_flag import SegmentFlag  # noqa: F401
        from models.speaker_assignment import SpeakerAssignment  # noqa: F401
        from services.output_analysis_gate import get_approved_ai_analyses_for_project
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()

        project_1 = Project(name="Output Analysis Gate Smoke")
        project_2 = Project(name="Other Project")
        db.session.add_all([project_1, project_2])
        db.session.flush()

        approved = AIAnalysis(
            project_id=project_1.id,
            interview_id=None,
            question_id=None,
            analysis_type="integrated",
            title="approved integrated",
            summary_text="approved summary",
            content_json=json.dumps({
                "findings": [{"point": "approved"}],
                "source_segment_quotes": [{"segment_id": 101, "text": "approved evidence"}],
            }, ensure_ascii=False),
            quote_ids=json.dumps(["Q1", "Q2"]),
            source_segment_ids=json.dumps([101, 102]),
            prompt_version="v0.2-approved",
            input_hash="hash-approved",
            model_used="none",
            status="approved",
        )
        approved_other_type = AIAnalysis(
            project_id=project_1.id,
            analysis_type="per_participant",
            title="approved participant",
            summary_text="approved participant summary",
            content_json=json.dumps({"kind": "participant"}, ensure_ascii=False),
            quote_ids=json.dumps(["QP1"]),
            source_segment_ids=json.dumps([201]),
            model_used="none",
            status="approved",
        )
        broken_json = AIAnalysis(
            project_id=project_1.id,
            analysis_type="integrated",
            title="approved broken json",
            summary_text="broken json summary",
            content_json="{not-json",
            quote_ids="not-json",
            source_segment_ids="not-json",
            model_used="none",
            status="approved",
        )
        draft = AIAnalysis(
            project_id=project_1.id,
            analysis_type="integrated",
            title="draft",
            content_json=json.dumps({"kind": "draft"}),
            status="draft",
        )
        reviewed = AIAnalysis(
            project_id=project_1.id,
            analysis_type="integrated",
            title="reviewed",
            content_json=json.dumps({"kind": "reviewed"}),
            status="reviewed",
        )
        rejected = AIAnalysis(
            project_id=project_1.id,
            analysis_type="integrated",
            title="rejected",
            content_json=json.dumps({"kind": "rejected"}),
            status="rejected",
        )
        other_project = AIAnalysis(
            project_id=project_2.id,
            analysis_type="integrated",
            title="other project approved",
            content_json=json.dumps({"kind": "other"}),
            status="approved",
        )
        db.session.add_all([
            approved,
            approved_other_type,
            broken_json,
            draft,
            reviewed,
            rejected,
            other_project,
        ])
        db.session.commit()

        count_before = AIAnalysis.query.count()

        rows = get_approved_ai_analyses_for_project(db.session, project_1.id)
        ids = {row["id"] for row in rows}

        failures += 0 if print_result(
            "approved rows are returned",
            approved.id in ids and approved_other_type.id in ids and broken_json.id in ids,
            f"ids={sorted(ids)}",
        ) else 1
        failures += 0 if print_result(
            "draft/reviewed/rejected rows are excluded",
            draft.id not in ids and reviewed.id not in ids and rejected.id not in ids,
            f"ids={sorted(ids)}",
        ) else 1
        failures += 0 if print_result(
            "other project approved row is excluded",
            other_project.id not in ids,
            f"ids={sorted(ids)}",
        ) else 1

        integrated_rows = get_approved_ai_analyses_for_project(
            db.session,
            project_1.id,
            analysis_type="integrated",
        )
        integrated_ids = {row["id"] for row in integrated_rows}
        failures += 0 if print_result(
            "analysis_type filter works",
            approved.id in integrated_ids
            and broken_json.id in integrated_ids
            and approved_other_type.id not in integrated_ids,
            f"ids={sorted(integrated_ids)}",
        ) else 1

        trace_required_rows = get_approved_ai_analyses_for_project(
            db.session,
            project_1.id,
            analysis_type="integrated",
            require_trace=True,
        )
        trace_required_ids = {row["id"] for row in trace_required_rows}
        failures += 0 if print_result(
            "require_trace keeps only rows with segment ids and source quotes",
            approved.id in trace_required_ids and broken_json.id not in trace_required_ids,
            f"ids={sorted(trace_required_ids)}",
        ) else 1

        approved_row = next((row for row in rows if row["id"] == approved.id), None)
        failures += 0 if print_result(
            "content_json is parsed as dict",
            bool(approved_row and isinstance(approved_row["content_json"], dict) and approved_row["content_json"].get("findings")),
        ) else 1
        failures += 0 if print_result(
            "quote_ids are parsed as list",
            bool(approved_row and approved_row["quote_ids"] == ["Q1", "Q2"]),
            f"value={approved_row['quote_ids'] if approved_row else None}",
        ) else 1
        failures += 0 if print_result(
            "source_segment_ids are parsed as list",
            bool(approved_row and approved_row["source_segment_ids"] == [101, 102]),
            f"value={approved_row['source_segment_ids'] if approved_row else None}",
        ) else 1

        broken_row = next((row for row in rows if row["id"] == broken_json.id), None)
        failures += 0 if print_result(
            "broken content_json does not raise",
            bool(broken_row and broken_row["content_json"].get("_parse_error") is True),
            f"value={broken_row['content_json'] if broken_row else None}",
        ) else 1
        failures += 0 if print_result(
            "broken quote/source ids become empty lists",
            bool(broken_row and broken_row["quote_ids"] == [] and broken_row["source_segment_ids"] == []),
            f"quote_ids={broken_row['quote_ids'] if broken_row else None}, source_segment_ids={broken_row['source_segment_ids'] if broken_row else None}",
        ) else 1

        required_keys = {
            "id",
            "project_id",
            "interview_id",
            "question_id",
            "analysis_type",
            "title",
            "summary_text",
            "content_json",
            "quote_ids",
            "source_segment_ids",
            "prompt_version",
            "input_hash",
            "model_used",
            "status",
            "created_at",
        }
        failures += 0 if print_result(
            "required fields are present",
            bool(rows and all(required_keys.issubset(set(row.keys())) for row in rows)),
        ) else 1

        count_after = AIAnalysis.query.count()
        failures += 0 if print_result(
            "helper does not update DB rows",
            count_before == count_after,
            f"before={count_before}, after={count_after}",
        ) else 1

    status_proc = run_git(repo_root, "status", "--short")
    if status_proc.returncode == 0:
        out = status_proc.stdout.strip()
        print("git status --short:")
        print(out if out else "(clean)")

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
