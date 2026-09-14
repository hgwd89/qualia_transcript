"""Read-only SQLite mirror of formal approved-analysis artifact currentness.

The normal delivery path uses SQLAlchemy models to decide whether a registered
``approved_analysis`` workbook still represents the complete current approved
analysis set. Production-readiness must make the same decision without starting
Flask, running migrations, or writing to the application database. This module
therefore mirrors the delivery contract using only a sqlite3-compatible
connection and plain JSON values.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from services.analysis_source_provenance_sqlite import (
    PROVENANCE_KEY,
    validate_analysis_source_provenance,
)


_REQUIRED_ANALYSIS_COLUMNS = {
    "id",
    "project_id",
    "interview_id",
    "question_id",
    "analysis_type",
    "title",
    "summary_text",
    "content_json",
    "model_used",
    "review_status",
    "review_note",
    "reviewed_at",
    "created_at",
}


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _analysis_content(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["content_json"] or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"AIAnalysis id={row['id']} content_json is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"AIAnalysis id={row['id']} content_json is not an object")
    return value


def _iso_sqlite_datetime(value: Any) -> str:
    """Mirror ``datetime.isoformat()`` on SQLAlchemy SQLite DateTime values."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid analysis datetime: {value!r}") from exc


def formal_analysis_state_sha256_sqlite(row: sqlite3.Row) -> str:
    """Hash the exact formal-workbook analysis fields from a SQLite query row."""
    content = _analysis_content(row)
    findings = content.get("findings") or []
    if not isinstance(findings, list):
        raise ValueError(f"AIAnalysis id={row['id']} findings is not an array")

    normalized_findings: list[dict[str, Any]] = []
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            raise ValueError(f"AIAnalysis id={row['id']} finding #{index} is not an object")
        normalized_findings.append({
            "point": finding.get("point", "") or "",
            "evidence_quote": finding.get("evidence_quote", "") or "",
            "source_segment_ids": list(finding.get("source_segment_ids") or []),
            "participant_codes": list(finding.get("participant_codes") or []),
            "question_codes": list(finding.get("question_codes") or []),
            "confidence": finding.get("confidence", "") or "",
        })

    state = {
        "analysis_id": int(row["id"]),
        "analysis_type": str(row["analysis_type"] or ""),
        "title": row["title"] or "",
        "participant_code": row["participant_code"] or "",
        "question_code": row["question_code"] or "",
        "summary_text": row["summary_text"] or "",
        "implications": content.get("implications", "") or "",
        "unresolved": content.get("unresolved", "") or "",
        "review_status": row["review_status"] or "",
        "review_note": row["review_note"] or "",
        "reviewed_at": _iso_sqlite_datetime(row["reviewed_at"]),
        "model_used": row["model_used"] or "",
        "created_at": _iso_sqlite_datetime(row["created_at"]),
        "findings": normalized_findings,
    }
    return hashlib.sha256(_canonical_json(state).encode("utf-8")).hexdigest()


def _generation_params(raw: str | None) -> dict[str, Any]:
    try:
        params = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("formal artifact generation metadata is invalid") from exc
    if not isinstance(params, dict):
        raise ValueError("formal artifact generation metadata is invalid")
    return params


def _valid_sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError("formal artifact SHA-256 metadata is missing or invalid")
    return text


def _analysis_columns(con: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in con.execute("PRAGMA table_info(ai_analyses)").fetchall()
    }


def _analysis_row(con: sqlite3.Connection, analysis_id: int) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT
            a.id,
            a.project_id,
            a.interview_id,
            a.question_id,
            a.analysis_type,
            a.title,
            a.summary_text,
            a.content_json,
            a.model_used,
            a.review_status,
            a.review_note,
            a.reviewed_at,
            a.created_at,
            p.participant_code AS participant_code,
            q.question_code AS question_code
        FROM ai_analyses a
        LEFT JOIN interviews i ON i.id=a.interview_id
        LEFT JOIN participants p ON p.id=i.participant_id
        LEFT JOIN interview_flow_questions q ON q.id=a.question_id
        WHERE a.id=?
        """,
        (int(analysis_id),),
    ).fetchone()


def validate_approved_analysis_artifact_currentness(
    con: sqlite3.Connection,
    *,
    project_id: int | None,
    generation_params_json: str | None,
) -> tuple[bool, str]:
    """Return whether one formal artifact still matches the current formal set.

    This mirrors ``approved_analysis_artifact_currentness`` from the ORM delivery
    path. Missing legacy metadata/schema fails closed for the artifact but never
    mutates or migrates the source database.
    """
    try:
        if project_id is None:
            return False, "formal artifact project identity is missing"
        project_id = int(project_id)
        if project_id <= 0:
            return False, "formal artifact project identity is invalid"

        params = _generation_params(generation_params_json)
        _valid_sha256(params.get("artifact_sha256"))
        if params.get("approved_only") is not True:
            return False, "formal artifact generation provenance is missing"

        raw_ids = params.get("analysis_ids")
        hashes = params.get("source_provenance_sha256")
        state_hashes = params.get("formal_analysis_state_sha256")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or not isinstance(hashes, dict)
            or not isinstance(state_hashes, dict)
        ):
            return False, "formal artifact analysis provenance/state metadata is missing"
        try:
            analysis_ids = [int(value) for value in raw_ids]
        except (TypeError, ValueError):
            return False, "formal artifact analysis IDs are invalid"
        if len(set(analysis_ids)) != len(analysis_ids):
            return False, "formal artifact analysis IDs are duplicated"
        if params.get("analysis_count") is not None:
            try:
                if int(params.get("analysis_count")) != len(analysis_ids):
                    return False, "formal artifact analysis count is inconsistent"
            except (TypeError, ValueError):
                return False, "formal artifact analysis count is invalid"

        columns = _analysis_columns(con)
        missing_columns = sorted(_REQUIRED_ANALYSIS_COLUMNS - columns)
        if missing_columns:
            return False, (
                "formal artifact currentness cannot be proven because ai_analyses schema is missing: "
                + ", ".join(missing_columns)
            )

        current_approved_ids = [
            int(row["id"])
            for row in con.execute(
                """
                SELECT id
                FROM ai_analyses
                WHERE project_id=? AND review_status='approved'
                ORDER BY id
                """,
                (project_id,),
            ).fetchall()
        ]
        if sorted(analysis_ids) != current_approved_ids:
            return False, "formal artifact no longer matches the current approved analysis set"

        for analysis_id in analysis_ids:
            row = _analysis_row(con, analysis_id)
            if row is None or int(row["project_id"] or -1) != project_id:
                return False, f"formal artifact analysis id={analysis_id} is missing or cross-project"
            if str(row["review_status"] or "") != "approved":
                return False, f"formal artifact analysis id={analysis_id} is no longer approved"

            content = _analysis_content(row)
            provenance = content.get(PROVENANCE_KEY)
            stored_hash = (
                str((provenance or {}).get("sha256") or "")
                if isinstance(provenance, dict)
                else ""
            )
            artifact_hash = str(hashes.get(str(int(analysis_id))) or "")
            if not stored_hash or artifact_hash != stored_hash:
                return False, (
                    f"formal artifact analysis id={analysis_id} does not match its exported source hash"
                )

            current_state_hash = formal_analysis_state_sha256_sqlite(row)
            artifact_state_hash = str(state_hashes.get(str(int(analysis_id))) or "")
            if not artifact_state_hash or artifact_state_hash != current_state_hash:
                return False, (
                    f"formal artifact analysis id={analysis_id} no longer matches its exported analysis/review state"
                )

            provenance_ok, provenance_reason = validate_analysis_source_provenance(
                con,
                analysis_type=str(row["analysis_type"] or ""),
                project_id=project_id,
                interview_id=(
                    int(row["interview_id"])
                    if row["interview_id"] is not None else None
                ),
                question_id=(
                    int(row["question_id"])
                    if row["question_id"] is not None else None
                ),
                content=content,
            )
            if not provenance_ok:
                return False, (
                    f"formal artifact analysis id={analysis_id} source is stale or unprovable: "
                    f"{provenance_reason}"
                )

        return True, ""
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        return False, f"formal artifact currentness is unprovable: {type(exc).__name__}: {exc}"
