import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


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
    path = repo_root / "scripts" / "audit_production_readiness.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_fixture(db_path: Path, output_dir: Path, backup_dir: Path) -> None:
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
    artifact.write_bytes(b"xlsx-fixture")

    raw_dir = output_dir / "raw_transcripts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "transcription_1_smoke.json").write_text(
        json.dumps({"transcription_id": 1, "interview_id": 1, "text": "raw"}),
        encoding="utf-8",
    )

    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "qualia_backup_smoke.zip").write_bytes(b"backup-fixture")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    audit_mod = load_audit(repo_root)
    failures = 0

    with tempfile.TemporaryDirectory(prefix="qualia_readiness_smoke_") as tmp:
        root = Path(tmp)
        db_path = root / "smoke.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        create_fixture(db_path, output_dir, backup_dir)

        before_hash = sha256(db_path)
        report = audit_mod.audit(db_path, output_dir, backup_dir)
        after_hash = sha256(db_path)
        failures += check("healthy fixture has no blockers", report["blockers"] == [], str(report["blockers"]))
        failures += check("healthy fixture has no warnings", report["warnings"] == [], str(report["warnings"]))
        failures += check("audit is read-only", before_hash == after_hash)

        artifact = output_dir / "1" / "deliverable.xlsx"
        artifact.unlink()
        missing_report = audit_mod.audit(db_path, output_dir, backup_dir)
        missing_codes = {item["code"] for item in missing_report["blockers"]}
        failures += check("missing registered output is a blocker", "generated_file_missing" in missing_codes, str(missing_codes))
        artifact.write_bytes(b"xlsx-fixture")

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
