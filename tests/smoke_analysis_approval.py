import json
import subprocess
import sys
import tempfile
from pathlib import Path


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


def make_analysis(project_id: int, analysis_type: str, title: str, status: str):
    from models.analysis import AIAnalysis

    return AIAnalysis(
        project_id=project_id,
        analysis_type=analysis_type,
        title=title,
        summary_text=f"{title} summary",
        content_json=json.dumps({
            "findings": [
                {
                    "point": f"{title} finding",
                    "evidence_quote": f"{title} quote",
                    "participant_codes": ["P01"],
                    "question_codes": ["Q1"],
                    "confidence": "medium",
                }
            ],
            "implications": f"{title} implication",
            "unresolved": "",
        }, ensure_ascii=False),
        model_used="none",
        status=status,
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    repo_state_before = {
        "instance": (repo_root / "instance").exists(),
        "uploads": (repo_root / "uploads").exists(),
        "outputs": (repo_root / "outputs").exists(),
    }

    try:
        import config
        from app import create_app
        from models import db
        from models.analysis import AIAnalysis
        from models.project import Project
    except Exception as e:
        print_result("imports", False, f"{type(e).__name__}: {e}")
        return 1

    original_database_uri = config.DATABASE_URI
    original_upload_dir = config.UPLOAD_DIR
    original_output_dir = config.OUTPUT_DIR

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            config.DATABASE_URI = f"sqlite:///{(tmp_root / 'analysis_approval_smoke.db').as_posix()}"
            config.UPLOAD_DIR = str(tmp_root / "uploads")
            config.OUTPUT_DIR = str(tmp_root / "outputs")

            app = create_app()
            client = app.test_client()

            failures += 0 if print_result(
                "temporary database configured",
                config.DATABASE_URI.startswith("sqlite:///") and config.DATABASE_URI.endswith("/analysis_approval_smoke.db"),
                config.DATABASE_URI,
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_root) in config.UPLOAD_DIR and str(tmp_root) in config.OUTPUT_DIR,
            ) else 1

            with app.app_context():
                project_1 = Project(name="Analysis Approval Smoke")
                project_2 = Project(name="Other Project")
                db.session.add_all([project_1, project_2])
                db.session.flush()

                draft = make_analysis(project_1.id, "per_participant", "draft analysis", "draft")
                reviewed = make_analysis(project_1.id, "cross_participant", "reviewed analysis", "reviewed")
                approved = make_analysis(project_1.id, "integrated", "approved analysis", "approved")
                rejected = make_analysis(project_1.id, "per_participant", "rejected analysis", "rejected")
                other_project = make_analysis(project_2.id, "integrated", "other project analysis", "draft")
                db.session.add_all([draft, reviewed, approved, rejected, other_project])
                db.session.commit()

                draft_id = draft.id
                reviewed_id = reviewed.id
                approved_id = approved.id
                rejected_id = rejected.id
                other_project_id = other_project.id
                project_1_id = project_1.id
                project_2_id = project_2.id

            r_get = client.get(f"/projects/{project_1_id}/analysis")
            html = r_get.get_data(as_text=True)
            failures += 0 if print_result(
                "GET analysis page returns 200",
                r_get.status_code == 200,
                f"status={r_get.status_code}",
            ) else 1
            failures += 0 if print_result(
                "status badges are rendered",
                all(token in html for token in ["status: draft", "status: reviewed", "status: approved", "status: rejected"]),
            ) else 1
            failures += 0 if print_result(
                "analysis anchors are rendered",
                all(f'id="analysis-{x}"' in html for x in [draft_id, reviewed_id, approved_id, rejected_id]),
            ) else 1
            failures += 0 if print_result(
                "status guidance text rendered",
                "Review Queue対象" in html and "正式出力対象候補" in html and "正式出力対象外" in html,
            ) else 1

            r_reviewed = client.post(
                f"/api/projects/{project_1_id}/analyses/{draft_id}/status",
                json={"status": "reviewed"},
            )
            reviewed_json = r_reviewed.get_json(silent=True) or {}
            with app.app_context():
                draft_row = db.session.get(AIAnalysis, draft_id)
                failures += 0 if print_result(
                    "POST reviewed works",
                    r_reviewed.status_code == 200 and draft_row.status == "reviewed",
                    f"status_code={r_reviewed.status_code}, json={reviewed_json}",
                ) else 1
                failures += 0 if print_result(
                    "reviewed metadata set",
                    draft_row.reviewed_by == "local_user" and draft_row.reviewed_at is not None,
                    f"reviewed_by={draft_row.reviewed_by}, reviewed_at={draft_row.reviewed_at}",
                ) else 1

            r_approved = client.post(
                f"/api/projects/{project_1_id}/analyses/{reviewed_id}/status",
                data={"status": "approved"},
                follow_redirects=False,
            )
            with app.app_context():
                reviewed_row = db.session.get(AIAnalysis, reviewed_id)
                failures += 0 if print_result(
                    "POST approved works",
                    r_approved.status_code in (302, 303) and reviewed_row.status == "approved",
                    f"status_code={r_approved.status_code}, value={reviewed_row.status}",
                ) else 1

            r_rejected = client.post(
                f"/api/projects/{project_1_id}/analyses/{approved_id}/status",
                json={"status": "rejected"},
            )
            with app.app_context():
                approved_row = db.session.get(AIAnalysis, approved_id)
                failures += 0 if print_result(
                    "POST rejected works",
                    r_rejected.status_code == 200 and approved_row.status == "rejected",
                    f"status_code={r_rejected.status_code}, value={approved_row.status}",
                ) else 1

            r_invalid = client.post(
                f"/api/projects/{project_1_id}/analyses/{rejected_id}/status",
                json={"status": "invalid"},
            )
            failures += 0 if print_result(
                "invalid status rejected",
                r_invalid.status_code == 400,
                f"status={r_invalid.status_code}",
            ) else 1

            r_draft = client.post(
                f"/api/projects/{project_1_id}/analyses/{rejected_id}/status",
                json={"status": "draft"},
            )
            failures += 0 if print_result(
                "draft reset rejected",
                r_draft.status_code == 400,
                f"status={r_draft.status_code}",
            ) else 1

            r_other = client.post(
                f"/api/projects/{project_1_id}/analyses/{other_project_id}/status",
                json={"status": "approved"},
            )
            failures += 0 if print_result(
                "other project analysis update rejected",
                r_other.status_code == 404,
                f"status={r_other.status_code}",
            ) else 1

            with app.app_context():
                db.session.remove()
                db.engine.dispose()
    finally:
        config.DATABASE_URI = original_database_uri
        config.UPLOAD_DIR = original_upload_dir
        config.OUTPUT_DIR = original_output_dir

    repo_state_after = {
        "instance": (repo_root / "instance").exists(),
        "uploads": (repo_root / "uploads").exists(),
        "outputs": (repo_root / "outputs").exists(),
    }
    failures += 0 if print_result(
        "repo instance/uploads/outputs state unchanged",
        repo_state_before == repo_state_after,
        f"before={repo_state_before}, after={repo_state_after}",
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
