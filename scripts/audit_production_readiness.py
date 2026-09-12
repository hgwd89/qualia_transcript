"""Read-only production readiness audit for real Qualia Transcript data.

This command never calls OpenAI/Whisper and never writes to the application DB.
It checks the local dataset and registered deliverables for conditions that would
block professional use or require human review before delivery.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote


REQUIRED_TABLES = {
    "projects",
    "participants",
    "interviews",
    "interview_flows",
    "interview_flow_sections",
    "interview_flow_questions",
    "media_files",
    "transcriptions",
    "segments",
    "utterance_mappings",
    "speaker_assignments",
    "ai_analyses",
    "generated_files",
}
ALLOWED_SPEAKER_ROLES = {"moderator", "respondent", "observer", "unknown"}
FINAL_INTERVIEW_STATUSES = {"mapped", "analyzed", "done"}


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_db_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    root = _root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import config

    uri = str(getattr(config, "DATABASE_URI", ""))
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise ValueError("production readiness audit currently supports sqlite:/// only")
    raw = unquote(uri[len(prefix):])
    path = Path(raw)
    if not path.is_absolute():
        path = Path(getattr(config, "BASE_DIR", root)) / path
    return path.resolve()


def _resolve_output_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = _root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import config
    return Path(config.OUTPUT_DIR).resolve()


def _resolve_backup_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = _root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import config
    return Path(getattr(config, "BACKUP_DIR", root / "backups")).resolve()


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _normalize_text(value: str | None) -> str:
    text = str(value or "").strip()
    if "「" in text and "」" in text and text.find("「") < text.rfind("」"):
        text = text[text.find("「") + 1:text.rfind("」")]
    text = text.strip(' \t\r\n"\'“”‘’「」『』')
    return re.sub(r"\s+", " ", text).strip()


def _safe_output_path(output_root: Path, stored_path: str) -> Path | None:
    if not stored_path:
        return None
    candidate = (output_root / stored_path).resolve()
    root = output_root.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def _issue(bucket: list[dict], code: str, message: str, **context) -> None:
    item = {"code": code, "message": message}
    if context:
        item["context"] = context
    bucket.append(item)


def _load_raw_snapshots(output_dir: Path) -> tuple[dict[int, list[dict]], list[dict]]:
    by_transcription: dict[int, list[dict]] = defaultdict(list)
    malformed: list[dict] = []
    raw_dir = output_dir / "raw_transcripts"
    if not raw_dir.is_dir():
        return by_transcription, malformed

    for path in sorted(raw_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            tid = payload.get("transcription_id")
            if isinstance(tid, int):
                by_transcription[tid].append({"path": str(path), "payload": payload})
            else:
                malformed.append({"path": str(path), "reason": "transcription_id missing/non-integer"})
        except Exception as exc:
            malformed.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
    return by_transcription, malformed


def audit(
    db_path: Path,
    output_dir: Path,
    backup_dir: Path,
) -> dict:
    blockers: list[dict] = []
    warnings: list[dict] = []
    info: dict = {
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "backup_dir": str(backup_dir),
    }

    con = _connect_ro(db_path)
    try:
        tables = _tables(con)
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            _issue(blockers, "missing_tables", "Required tables are missing", tables=missing)
            return {"blockers": blockers, "warnings": warnings, "info": info}

        counts = {}
        for table in sorted(REQUIRED_TABLES):
            counts[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        info["table_counts"] = counts

        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            _issue(blockers, "sqlite_integrity", "SQLite integrity_check failed", result=integrity[0] if integrity else None)

        empty_segments = con.execute(
            "SELECT id, interview_id FROM segments WHERE text IS NULL OR TRIM(text)='' ORDER BY id"
        ).fetchall()
        if empty_segments:
            _issue(blockers, "empty_segment_text", "Segments with empty source text exist", segment_ids=[r["id"] for r in empty_segments[:100]], count=len(empty_segments))

        invalid_roles = con.execute(
            "SELECT id, interview_id, speaker_role FROM segments WHERE speaker_role IS NOT NULL"
        ).fetchall()
        invalid_roles = [r for r in invalid_roles if str(r["speaker_role"]) not in ALLOWED_SPEAKER_ROLES]
        if invalid_roles:
            _issue(blockers, "invalid_speaker_role", "Segments contain unsupported speaker roles", rows=[dict(r) for r in invalid_roles[:100]], count=len(invalid_roles))

        unknown_count = int(con.execute(
            "SELECT COUNT(*) FROM segments WHERE speaker_role IS NULL OR speaker_role='unknown'"
        ).fetchone()[0])
        if unknown_count:
            _issue(warnings, "unknown_speakers", "Segments still have unknown speaker role", count=unknown_count)

        cross_interview_participant = con.execute(
            """
            SELECT i.id AS interview_id, i.project_id, i.participant_id, p.project_id AS participant_project_id
            FROM interviews i
            JOIN participants p ON p.id=i.participant_id
            WHERE i.participant_id IS NOT NULL AND p.project_id<>i.project_id
            """
        ).fetchall()
        if cross_interview_participant:
            _issue(blockers, "interview_participant_project_mismatch", "Interview references participant from another project", rows=[dict(r) for r in cross_interview_participant[:100]])

        cross_segment_participant = con.execute(
            """
            SELECT s.id AS segment_id, i.project_id, s.participant_id, p.project_id AS participant_project_id
            FROM segments s
            JOIN interviews i ON i.id=s.interview_id
            JOIN participants p ON p.id=s.participant_id
            WHERE s.participant_id IS NOT NULL AND p.project_id<>i.project_id
            """
        ).fetchall()
        if cross_segment_participant:
            _issue(blockers, "segment_participant_project_mismatch", "Segment references participant from another project", rows=[dict(r) for r in cross_segment_participant[:100]])

        cross_assignment_participant = con.execute(
            """
            SELECT sa.id AS assignment_id, sa.interview_id, i.project_id, sa.participant_id,
                   p.project_id AS participant_project_id
            FROM speaker_assignments sa
            JOIN interviews i ON i.id=sa.interview_id
            JOIN participants p ON p.id=sa.participant_id
            WHERE sa.participant_id IS NOT NULL AND p.project_id<>i.project_id
            """
        ).fetchall()
        if cross_assignment_participant:
            _issue(blockers, "speaker_assignment_project_mismatch", "Speaker assignment references participant from another project", rows=[dict(r) for r in cross_assignment_participant[:100]])

        wrong_flow_mappings = con.execute(
            """
            SELECT um.id AS mapping_id, s.id AS segment_id, i.id AS interview_id,
                   i.flow_id AS interview_flow_id, q.id AS question_id, sec.flow_id AS question_flow_id
            FROM utterance_mappings um
            JOIN segments s ON s.id=um.segment_id
            JOIN interviews i ON i.id=s.interview_id
            LEFT JOIN interview_flow_questions q ON q.id=um.question_id
            LEFT JOIN interview_flow_sections sec ON sec.id=q.section_id
            WHERE um.question_id IS NOT NULL
              AND (
                  q.id IS NULL
                  OR i.flow_id IS NULL
                  OR sec.flow_id IS NULL
                  OR sec.flow_id<>i.flow_id
              )
            """
        ).fetchall()
        if wrong_flow_mappings:
            _issue(blockers, "mapping_flow_mismatch", "Mapped question does not belong to the interview flow", rows=[dict(r) for r in wrong_flow_mappings[:100]], count=len(wrong_flow_mappings))

        respondent_mapping_rows = con.execute(
            """
            SELECT s.id AS segment_id, s.interview_id,
                   COUNT(um.id) AS mapping_count,
                   SUM(CASE WHEN um.question_id IS NOT NULL AND COALESCE(um.is_unclassified,0)=0 THEN 1 ELSE 0 END) AS classified_count
            FROM segments s
            LEFT JOIN utterance_mappings um ON um.segment_id=s.id
            WHERE s.speaker_role='respondent'
            GROUP BY s.id, s.interview_id
            """
        ).fetchall()
        unmapped = [dict(r) for r in respondent_mapping_rows if int(r["mapping_count"] or 0) == 0]
        unclassified = [dict(r) for r in respondent_mapping_rows if int(r["mapping_count"] or 0) > 0 and int(r["classified_count"] or 0) == 0]
        if unmapped:
            _issue(warnings, "respondent_without_mapping", "Respondent segments have no UtteranceMapping", segment_ids=[r["segment_id"] for r in unmapped[:200]], count=len(unmapped))
        if unclassified:
            _issue(warnings, "respondent_unclassified", "Respondent segments remain unclassified", segment_ids=[r["segment_id"] for r in unclassified[:200]], count=len(unclassified))

        interview_rows = con.execute(
            "SELECT id, project_id, status FROM interviews ORDER BY project_id, id"
        ).fetchall()
        project_summary: dict[int, dict] = defaultdict(lambda: {"interviews": 0, "segments": 0, "approved_analyses": 0, "generated_files": 0})
        for r in interview_rows:
            project_summary[int(r["project_id"])]["interviews"] += 1
            seg_count = int(con.execute("SELECT COUNT(*) FROM segments WHERE interview_id=?", (r["id"],)).fetchone()[0])
            project_summary[int(r["project_id"])]["segments"] += seg_count
            if str(r["status"] or "") in FINAL_INTERVIEW_STATUSES and seg_count == 0:
                _issue(warnings, "final_interview_without_segments", "Interview is in a downstream status but has zero segments", interview_id=r["id"], status=r["status"])

        approved_rows = con.execute(
            """
            SELECT id, project_id, interview_id, analysis_type, content_json
            FROM ai_analyses
            WHERE review_status='approved'
            ORDER BY id
            """
        ).fetchall()
        info["approved_analysis_count"] = len(approved_rows)
        for row in approved_rows:
            aid = int(row["id"])
            project_id = int(row["project_id"]) if row["project_id"] is not None else None
            if project_id is not None:
                project_summary[project_id]["approved_analyses"] += 1
            try:
                content = json.loads(row["content_json"] or "{}")
            except Exception as exc:
                _issue(blockers, "approved_analysis_invalid_json", "Approved AIAnalysis content_json is invalid", analysis_id=aid, error=f"{type(exc).__name__}: {exc}")
                continue
            findings = content.get("findings") if isinstance(content, dict) else None
            if not isinstance(findings, list) or not findings:
                _issue(blockers, "approved_analysis_no_findings", "Approved AIAnalysis has no findings", analysis_id=aid)
                continue
            for idx, finding in enumerate(findings, start=1):
                if not isinstance(finding, dict):
                    _issue(blockers, "approved_finding_invalid", "Approved finding is not an object", analysis_id=aid, finding_no=idx)
                    continue
                quote = _normalize_text(finding.get("evidence_quote"))
                source_ids = finding.get("source_segment_ids") or []
                if not quote or not isinstance(source_ids, list) or not source_ids:
                    _issue(blockers, "approved_finding_missing_evidence", "Approved finding lacks evidence_quote/source_segment_ids", analysis_id=aid, finding_no=idx)
                    continue
                matched_source_count = 0
                for raw_sid in source_ids:
                    try:
                        sid = int(raw_sid)
                    except (TypeError, ValueError):
                        _issue(blockers, "approved_source_id_invalid", "source_segment_ids contains non-integer value", analysis_id=aid, finding_no=idx, value=raw_sid)
                        continue
                    seg = con.execute(
                        """
                        SELECT s.id, s.interview_id, s.speaker_role, s.text, i.project_id
                        FROM segments s JOIN interviews i ON i.id=s.interview_id
                        WHERE s.id=?
                        """,
                        (sid,),
                    ).fetchone()
                    if not seg:
                        _issue(blockers, "approved_source_missing", "Approved source segment does not exist", analysis_id=aid, finding_no=idx, segment_id=sid)
                        continue
                    if seg["speaker_role"] != "respondent":
                        _issue(blockers, "approved_source_not_respondent", "Approved evidence references non-respondent segment", analysis_id=aid, finding_no=idx, segment_id=sid, role=seg["speaker_role"])
                    if project_id is not None and int(seg["project_id"]) != project_id:
                        _issue(blockers, "approved_source_project_mismatch", "Approved evidence references another project", analysis_id=aid, finding_no=idx, segment_id=sid)
                    if row["interview_id"] is not None and int(seg["interview_id"]) != int(row["interview_id"]):
                        _issue(blockers, "approved_source_interview_mismatch", "Interview-scoped analysis references another interview", analysis_id=aid, finding_no=idx, segment_id=sid)
                    source_text = _normalize_text(seg["text"])
                    source_matches_quote = quote == source_text or (
                        len(quote) >= 8 and (quote in source_text or source_text in quote)
                    )
                    if source_matches_quote:
                        matched_source_count += 1
                    else:
                        _issue(
                            blockers,
                            "approved_source_quote_mismatch",
                            "Approved source segment does not match the finding evidence_quote",
                            analysis_id=aid,
                            finding_no=idx,
                            segment_id=sid,
                        )
                if matched_source_count == 0:
                    _issue(blockers, "approved_quote_not_in_sources", "Approved evidence_quote does not match referenced source segments", analysis_id=aid, finding_no=idx)

        output_root = output_dir.resolve()
        generated = con.execute(
            "SELECT id, project_id, interview_id, file_type, file_format, stored_path FROM generated_files ORDER BY id"
        ).fetchall()
        info["generated_file_count"] = len(generated)
        for row in generated:
            pid = int(row["project_id"])
            project_summary[pid]["generated_files"] += 1
            path = _safe_output_path(output_root, str(row["stored_path"] or ""))
            if path is None:
                _issue(blockers, "generated_file_unsafe_path", "GeneratedFile stored_path escapes output directory", generated_file_id=row["id"], stored_path=row["stored_path"])
                continue
            if not path.is_file():
                _issue(blockers, "generated_file_missing", "GeneratedFile DB row points to missing artifact", generated_file_id=row["id"], path=str(path))
                continue
            if path.stat().st_size <= 0:
                _issue(blockers, "generated_file_empty", "Generated artifact is zero bytes", generated_file_id=row["id"], path=str(path))
            fmt = str(row["file_format"] or "").lower().strip()
            if fmt and path.suffix.lower() != f".{fmt}":
                _issue(warnings, "generated_file_extension_mismatch", "GeneratedFile extension differs from file_format", generated_file_id=row["id"], file_format=fmt, path=str(path))

        raw_by_tid, malformed_raw = _load_raw_snapshots(output_dir)
        info["raw_transcript_snapshot_count"] = sum(len(v) for v in raw_by_tid.values())
        if malformed_raw:
            _issue(blockers, "malformed_raw_transcript_snapshot", "Raw transcript snapshot JSON is malformed", files=malformed_raw[:100], count=len(malformed_raw))

        done_transcriptions = con.execute(
            "SELECT id FROM transcriptions WHERE status='done' ORDER BY id"
        ).fetchall()
        missing_raw = [int(r["id"]) for r in done_transcriptions if int(r["id"]) not in raw_by_tid]
        if missing_raw:
            _issue(warnings, "done_transcription_without_raw_snapshot", "Completed transcriptions have no raw snapshot", transcription_ids=missing_raw[:200], count=len(missing_raw))

        backups = sorted(backup_dir.glob("qualia_backup_*.zip")) if backup_dir.is_dir() else []
        info["backup_count"] = len(backups)
        info["latest_backup"] = str(backups[-1]) if backups else None
        if not backups:
            _issue(warnings, "no_backup_archive", "No local backup archive exists yet")

        analyzed_without_approved = con.execute(
            """
            SELECT i.id, i.project_id, i.status
            FROM interviews i
            WHERE i.status IN ('analyzed','done')
              AND NOT EXISTS (
                  SELECT 1 FROM ai_analyses a
                  WHERE a.interview_id=i.id AND a.review_status='approved'
              )
            ORDER BY i.id
            """
        ).fetchall()
        if analyzed_without_approved:
            _issue(warnings, "analyzed_interview_without_approved_analysis", "Analyzed/done interviews have no approved AIAnalysis", interview_ids=[r["id"] for r in analyzed_without_approved[:200]], count=len(analyzed_without_approved))

        info["project_summary"] = {str(k): v for k, v in sorted(project_summary.items())}
        info["warning_counts"] = dict(Counter(item["code"] for item in warnings))
        info["blocker_counts"] = dict(Counter(item["code"] for item in blockers))
    finally:
        con.close()

    return {"blockers": blockers, "warnings": warnings, "info": info}


def _print_report(report: dict) -> None:
    info = report["info"]
    print("Qualia Transcript production readiness audit")
    print(f"DB: {info.get('db_path')}")
    print(f"Outputs: {info.get('output_dir')}")
    print(f"Backups: {info.get('backup_dir')}")
    print()
    for item in report["blockers"]:
        print(f"[BLOCKER] {item['code']}: {item['message']}")
        if item.get("context"):
            print("          " + json.dumps(item["context"], ensure_ascii=False, default=str))
    for item in report["warnings"]:
        print(f"[WARN] {item['code']}: {item['message']}")
        if item.get("context"):
            print("       " + json.dumps(item["context"], ensure_ascii=False, default=str))
    print()
    print(f"Summary: blockers={len(report['blockers'])} warnings={len(report['warnings'])}")
    if not report["blockers"] and not report["warnings"]:
        print("READY: no blocking or warning conditions detected.")
    elif not report["blockers"]:
        print("CONDITIONALLY READY: no blockers, but warnings require review.")
    else:
        print("NOT READY: blocking conditions must be resolved before professional delivery.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only production readiness audit")
    parser.add_argument("--db", help="SQLite DB path; defaults to config.DATABASE_URI")
    parser.add_argument("--output-dir", help="outputs directory; defaults to config.OUTPUT_DIR")
    parser.add_argument("--backup-dir", help="backup directory; defaults to config.BACKUP_DIR")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument("--strict", action="store_true", help="treat warnings as a failing exit status")
    args = parser.parse_args()

    root = _root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from services.runtime_lock import RuntimeLockError, runtime_lock

    try:
        with runtime_lock("reader"):
            try:
                report = audit(
                    _resolve_db_path(args.db),
                    _resolve_output_dir(args.output_dir),
                    _resolve_backup_dir(args.backup_dir),
                )
            except Exception as exc:
                report = {
                    "blockers": [{"code": "audit_error", "message": f"{type(exc).__name__}: {exc}"}],
                    "warnings": [],
                    "info": {},
                }
    except RuntimeLockError as exc:
        report = {
            "blockers": [{
                "code": "maintenance_active",
                "message": "Readiness audit refused while backup/restore maintenance is active",
                "context": {"error": str(exc)},
            }],
            "warnings": [],
            "info": {},
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            _print_report(report)
        return 3

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(report)

    if report["blockers"]:
        return 1
    if args.strict and report["warnings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
