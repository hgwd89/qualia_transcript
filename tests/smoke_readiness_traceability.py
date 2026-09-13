import hashlib
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


def load_hardened_audit(repo_root: Path):
    scripts_dir = repo_root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script = scripts_dir / "audit_production_readiness_v2.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_v2_traceability", script)
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
            CREATE TABLE transcriptions (
                id INTEGER PRIMARY KEY,
                media_file_id INTEGER,
                status TEXT,
                started_at DATETIME,
                completed_at DATETIME
            );
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


def warning_codes(report: dict) -> set[str]:
    return {str(item.get("code")) for item in report.get("warnings", [])}


def write_raw_snapshot(
    path: Path,
    *,
    transcription_id: int,
    interview_id: int,
    created_at_utc: str,
    text: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "transcription_id": transcription_id,
                "interview_id": interview_id,
                "created_at_utc": created_at_utc,
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    audit_mod = load_base_audit(repo_root)
    hardened_audit_mod = load_hardened_audit(repo_root)

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

        # Raw snapshots are intentionally retained after project/transcription
        # deletion. Prove that reusing the same SQLite integer ID cannot cause a
        # predecessor snapshot to satisfy readiness for the replacement row.
        con = sqlite3.connect(db_path)
        try:
            con.execute("INSERT INTO media_files VALUES (1, 1)")
            con.execute(
                "INSERT INTO transcriptions VALUES (1, 1, 'done', ?, ?)",
                ("2026-09-13 00:00:00", "2026-09-13 00:10:00"),
            )
            con.commit()
        finally:
            con.close()

        raw_dir = output_dir / "raw_transcripts"
        predecessor_snapshot = raw_dir / "transcription_1_predecessor.json"
        write_raw_snapshot(
            predecessor_snapshot,
            transcription_id=1,
            interview_id=1,
            created_at_utc="2026-09-13T00:05:00+00:00",
            text="immutable predecessor source",
        )
        predecessor_bytes = predecessor_snapshot.read_bytes()

        original_base = audit_mod.audit(db_path, output_dir, backup_dir)
        original_hardened = hardened_audit_mod.audit(db_path, output_dir, backup_dir)
        failures += check(
            "current-generation raw snapshot satisfies both readiness audits",
            "done_transcription_without_raw_snapshot" not in warning_codes(original_base)
            and "done_transcription_without_raw_snapshot" not in warning_codes(original_hardened),
            f"base={warning_codes(original_base)} hardened={warning_codes(original_hardened)}",
        )

        con = sqlite3.connect(db_path)
        try:
            con.execute("DELETE FROM transcriptions WHERE id=1")
            con.execute(
                "INSERT INTO transcriptions VALUES (1, 1, 'done', ?, ?)",
                ("2026-09-13 01:00:00", "2026-09-13 01:10:00"),
            )
            con.commit()
        finally:
            con.close()

        reused_base = audit_mod.audit(db_path, output_dir, backup_dir)
        reused_hardened = hardened_audit_mod.audit(db_path, output_dir, backup_dir)
        failures += check(
            "retained predecessor snapshot cannot mask missing snapshot after transcription ID reuse",
            "done_transcription_without_raw_snapshot" in warning_codes(reused_base)
            and "done_transcription_without_raw_snapshot" in warning_codes(reused_hardened),
            f"base={warning_codes(reused_base)} hardened={warning_codes(reused_hardened)}",
        )
        failures += check(
            "ID-reuse readiness check leaves immutable predecessor bytes unchanged",
            predecessor_snapshot.read_bytes() == predecessor_bytes,
        )

        replacement_snapshot = raw_dir / "transcription_1_replacement.json"
        write_raw_snapshot(
            replacement_snapshot,
            transcription_id=1,
            interview_id=1,
            created_at_utc="2026-09-13T01:05:00+00:00",
            text="replacement generation source",
        )
        replacement_base = audit_mod.audit(db_path, output_dir, backup_dir)
        replacement_hardened = hardened_audit_mod.audit(db_path, output_dir, backup_dir)
        failures += check(
            "replacement generation requires and accepts its own raw snapshot",
            "done_transcription_without_raw_snapshot" not in warning_codes(replacement_base)
            and "done_transcription_without_raw_snapshot" not in warning_codes(replacement_hardened),
            f"base={warning_codes(replacement_base)} hardened={warning_codes(replacement_hardened)}",
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())