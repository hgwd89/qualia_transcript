import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def load_base_audit(repo_root: Path):
    script = repo_root / "scripts" / "audit_production_readiness.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_traceability", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_schema(db_path: Path) -> None:
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
        con.execute("INSERT INTO projects VALUES (1, 'Readiness Traceability')")
        con.execute("INSERT INTO participants VALUES (1, 1)")
        con.execute("INSERT INTO interview_flows VALUES (1, 1)")
        con.execute("INSERT INTO interview_flow_sections VALUES (1, 1)")
        con.execute("INSERT INTO interview_flow_questions VALUES (1, 1)")
        con.commit()
    finally:
        con.close()


def blocker_codes(report: dict) -> set[str]:
    return {str(item.get("code")) for item in report.get("blockers", [])}


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    audit_mod = load_base_audit(repo_root)

    with tempfile.TemporaryDirectory(prefix="qualia_readiness_traceability_") as tmp:
        root = Path(tmp)
        db_path = root / "traceability.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        output_dir.mkdir()
        backup_dir.mkdir()
        create_schema(db_path)

        con = sqlite3.connect(db_path)
        try:
            con.execute("INSERT INTO interviews VALUES (1, 1, 1, NULL, 'pending')")
            con.execute(
                "INSERT INTO segments VALUES (1, 1, 1, 'respondent', '回答です。')"
            )
            con.execute("INSERT INTO utterance_mappings VALUES (1, 1, 1, 0)")
            con.commit()
        finally:
            con.close()

        no_flow_report = audit_mod.audit(db_path, output_dir, backup_dir)
        no_flow_codes = blocker_codes(no_flow_report)
        failures += check(
            "question mapping is rejected when the interview has no assigned flow",
            "mapping_flow_mismatch" in no_flow_codes,
            str(no_flow_codes),
        )

        con = sqlite3.connect(db_path)
        try:
            con.execute("UPDATE interviews SET flow_id=1 WHERE id=1")
            con.execute(
                "INSERT INTO segments VALUES (2, 1, 1, 'respondent', '保湿すると安心します。')"
            )
            con.execute(
                "INSERT INTO segments VALUES (3, 1, 1, 'respondent', '価格は少し高いです。')"
            )
            con.execute("INSERT INTO utterance_mappings VALUES (2, 2, 1, 0)")
            con.execute("INSERT INTO utterance_mappings VALUES (3, 3, 1, 0)")
            content = {
                "findings": [
                    {
                        "point": "保湿が安心感につながる",
                        "evidence_quote": "保湿すると安心します。",
                        "source_segment_ids": [2, 3],
                    }
                ]
            }
            con.execute(
                "INSERT INTO ai_analyses VALUES (1, 1, 1, 'per_question', ?, 'approved')",
                (json.dumps(content, ensure_ascii=False),),
            )
            con.commit()
        finally:
            con.close()

        evidence_report = audit_mod.audit(db_path, output_dir, backup_dir)
        evidence_codes = blocker_codes(evidence_report)
        mismatches = [
            item for item in evidence_report["blockers"]
            if item.get("code") == "approved_source_quote_mismatch"
        ]
        failures += check(
            "every approved source segment must match the finding evidence quote",
            "approved_source_quote_mismatch" in evidence_codes
            and any((item.get("context") or {}).get("segment_id") == 3 for item in mismatches),
            str(mismatches),
        )
        failures += check(
            "a matching source does not hide an unrelated extra source ID",
            not any((item.get("context") or {}).get("segment_id") == 2 for item in mismatches)
            and any((item.get("context") or {}).get("segment_id") == 3 for item in mismatches),
            str(mismatches),
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
