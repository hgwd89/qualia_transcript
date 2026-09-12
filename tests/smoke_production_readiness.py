import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from openpyxl import Workbook


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def load_audit(repo_root: Path):
    scripts_dir = repo_root / "scripts"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    path = scripts_dir / "audit_production_readiness_v2.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_v2", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_fixture(db_path: Path, output_dir: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE participants (id INTEGER PRIMARY KEY, project_id INTEGER);
            CREATE TABLE interview_flows (id INTEGER PRIMARY KEY, project_id INTEGER);
            CREATE TABLE interview_flow_sections (id INTEGER PRIMARY KEY, flow_id INTEGER);
            CREATE TABLE interview_flow_questions (id INTEGER PRIMARY KEY, section_id INTEGER);
            CREATE TABLE interviews (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                participant_id INTEGER,
                flow_id INTEGER,
                status TEXT
            );
            CREATE TABLE media_files (id INTEGER PRIMARY KEY, interview_id INTEGER);
            CREATE TABLE transcriptions (id INTEGER PRIMARY KEY, media_file_id INTEGER, status TEXT);
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY,
                interview_id INTEGER,
                participant_id INTEGER,
                speaker_role TEXT,
                text TEXT
            );
            CREATE TABLE utterance_mappings (
                id INTEGER PRIMARY KEY,
                segment_id INTEGER,
                question_id INTEGER,
                is_unclassified INTEGER
            );
            CREATE TABLE speaker_assignments (
                id INTEGER PRIMARY KEY,
                interview_id INTEGER,
                participant_id INTEGER
            );
            CREATE TABLE ai_analyses (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                interview_id INTEGER,
                analysis_type TEXT,
                content_json TEXT,
                review_status TEXT
            );
            CREATE TABLE generated_files (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                interview_id INTEGER,
                file_type TEXT,
                file_format TEXT,
                stored_path TEXT
            );
            CREATE TABLE processing_jobs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                interview_id INTEGER,
                question_id INTEGER REFERENCES interview_flow_questions(id),
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                progress_json TEXT,
                result_json TEXT,
                error_message TEXT,
                attempt_count INTEGER DEFAULT 0,
                worker_pid INTEGER,
                created_at DATETIME,
                started_at DATETIME,
                finished_at DATETIME,
                updated_at DATETIME
            );
            """
        )
        con.execute("INSERT INTO projects VALUES (1, 'Smoke Project')")
        con.execute("INSERT INTO participants VALUES (1, 1)")
        con.execute("INSERT INTO interview_flows VALUES (1, 1)")
        con.execute("INSERT INTO interview_flow_sections VALUES (1, 1)")
        con.execute("INSERT INTO interview_flow_questions VALUES (1, 1)")
        con.execute("INSERT INTO interviews VALUES (1, 1, 1, 1, 'done')")
        con.execute("INSERT INTO media_files VALUES (1, 1)")
        con.execute("INSERT INTO transcriptions VALUES (1, 1, 'done')")
        con.execute("INSERT INTO segments VALUES (1, 1, NULL, 'moderator', '質問です。')")
        con.execute("INSERT INTO segments VALUES (2, 1, 1, 'respondent', '保湿すると安心します。')")
        con.execute("INSERT INTO utterance_mappings VALUES (1, 2, 1, 0)")
        con.execute("INSERT INTO speaker_assignments VALUES (1, 1, 1)")
        content = {
            "findings": [
                {
                    "point": "保湿が安心感につながる",
                    "evidence_quote": "保湿すると安心します。",
                    "source_segment_ids": [2],
                    "participant_codes": ["P01"],
                    "question_codes": ["Q1"],
                    "confidence": "high",
                }
            ],
            "implications": "安心感の価値がある",
            "unresolved": "",
        }
        con.execute(
            "INSERT INTO ai_analyses VALUES (1, 1, 1, 'per_question', ?, 'approved')",
            (json.dumps(content, ensure_ascii=False),),
        )
        con.execute(
            "INSERT INTO generated_files VALUES (1, 1, 1, 'approved_analysis', 'xlsx', '1/deliverable.xlsx')"
        )
        con.commit()
    finally:
        con.close()

    artifact = output_dir / "1" / "deliverable.xlsx"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.active["A1"] = "professional smoke"
    workbook.save(artifact)

    raw_text = "raw"
    raw_dir = output_dir / "raw_transcripts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcription_1_smoke.json").write_text(
        json.dumps(
            {
                "transcription_id": 1,
                "interview_id": 1,
                "text": raw_text,
                "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def save_valid_xlsx(path: Path) -> None:
    workbook = Workbook()
    workbook.active["A1"] = "restored valid workbook"
    workbook.save(path)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    audit_mod = load_audit(repo_root)
    from services.local_backup import create_backup

    failures = 0

    with tempfile.TemporaryDirectory(prefix="qualia_readiness_smoke_") as tmp:
        root = Path(tmp)
        db_path = root / "smoke.db"
        upload_dir = root / "uploads"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        upload_dir.mkdir(parents=True)
        (upload_dir / "fixture.txt").write_text("upload fixture", encoding="utf-8")
        create_fixture(db_path, output_dir)

        create_backup(
            backup_dir,
            database_uri=f"sqlite:///{db_path.as_posix()}",
            upload_dir=upload_dir,
            output_dir=output_dir,
            label="readiness_smoke",
        )

        before_hash = sha256(db_path)
        report = audit_mod.audit(db_path, output_dir, backup_dir)
        after_hash = sha256(db_path)
        failures += check("healthy fixture has no blockers", report["blockers"] == [], str(report["blockers"]))
        failures += check("healthy fixture has no warnings", report["warnings"] == [], str(report["warnings"]))
        failures += check("audit is read-only", before_hash == after_hash)

        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT INTO processing_jobs(id, project_id, interview_id, job_type, status, progress_json) VALUES (1, 1, 1, 'analyze', 'running', '{\"stage\":\"analyzing\"}')"
            )
            con.commit()
        finally:
            con.close()
        active_report = audit_mod.audit(db_path, output_dir, backup_dir)
        active_warning_codes = {item["code"] for item in active_report["warnings"]}
        failures += check(
            "active processing job is a readiness warning",
            "active_processing_jobs" in active_warning_codes,
            str(active_warning_codes),
        )
        con = sqlite3.connect(db_path)
        try:
            con.execute("DELETE FROM processing_jobs")
            con.commit()
        finally:
            con.close()

        artifact = output_dir / "1" / "deliverable.xlsx"
        artifact.unlink()
        missing_report = audit_mod.audit(db_path, output_dir, backup_dir)
        missing_codes = {item["code"] for item in missing_report["blockers"]}
        failures += check("missing registered output is a blocker", "generated_file_missing" in missing_codes, str(missing_codes))

        artifact.write_bytes(b"not-an-xlsx")
        invalid_artifact_report = audit_mod.audit(db_path, output_dir, backup_dir)
        invalid_artifact_codes = {item["code"] for item in invalid_artifact_report["blockers"]}
        failures += check("corrupt Office artifact is a blocker", "generated_file_invalid" in invalid_artifact_codes, str(invalid_artifact_codes))
        save_valid_xlsx(artifact)

        raw_path = output_dir / "raw_transcripts" / "transcription_1_smoke.json"
        raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
        raw_payload["sha256"] = "0" * 64
        raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False), encoding="utf-8")
        raw_report = audit_mod.audit(db_path, output_dir, backup_dir)
        raw_codes = {item["code"] for item in raw_report["blockers"]}
        failures += check("raw snapshot hash mismatch is a blocker", "raw_snapshot_invalid" in raw_codes, str(raw_codes))
        raw_payload["sha256"] = hashlib.sha256(raw_payload["text"].encode("utf-8")).hexdigest()
        raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False), encoding="utf-8")

        con = sqlite3.connect(db_path)
        try:
            bad_content = {
                "findings": [
                    {
                        "point": "bad",
                        "evidence_quote": "原文に存在しない引用です。",
                        "source_segment_ids": [2],
                    }
                ]
            }
            con.execute(
                "UPDATE ai_analyses SET content_json=? WHERE id=1",
                (json.dumps(bad_content, ensure_ascii=False),),
            )
            con.commit()
        finally:
            con.close()

        evidence_report = audit_mod.audit(db_path, output_dir, backup_dir)
        evidence_codes = {item["code"] for item in evidence_report["blockers"]}
        failures += check("approved quote/source mismatch is a blocker", "approved_quote_not_in_sources" in evidence_codes, str(evidence_codes))

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
