"""Approved AIAnalysis extraction gate for formal outputs.

This module only reads AIAnalysis rows. It intentionally does not connect the
rows to report generation yet; formal output services can use this gate later
to avoid leaking draft/reviewed/rejected AI analyses into deliverables.
"""
from __future__ import annotations

import json
from typing import Any

from models.analysis import AIAnalysis


def _parse_json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {"_parse_error": True, "raw": str(value)}
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {"_parse_error": True, "raw": value}
    if isinstance(parsed, dict):
        return parsed
    return {"_parse_error": True, "raw": value}


def _parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _has_required_trace(row: dict[str, Any]) -> bool:
    content = row.get("content_json") or {}
    source_segment_ids = row.get("source_segment_ids") or []
    source_segment_quotes = content.get("source_segment_quotes") if isinstance(content, dict) else None
    return bool(source_segment_ids) and isinstance(source_segment_quotes, list) and bool(source_segment_quotes)


def get_approved_ai_analyses_for_project(
    session,
    project_id: int,
    analysis_type: str | None = None,
    require_trace: bool = False,
) -> list[dict[str, Any]]:
    """Return approved AIAnalysis rows for formal output use.

    Only status="approved" rows from the specified project are returned.
    draft/reviewed/rejected rows are intentionally excluded. When require_trace
    is true, rows without source_segment_ids and source_segment_quotes are also
    excluded from formal-output use.
    """
    query = (
        session.query(AIAnalysis)
        .filter_by(project_id=project_id, status="approved")
    )
    if analysis_type:
        query = query.filter_by(analysis_type=analysis_type)

    rows = query.order_by(AIAnalysis.created_at.asc(), AIAnalysis.id.asc()).all()
    result = [
        {
            "id": row.id,
            "project_id": row.project_id,
            "interview_id": row.interview_id,
            "question_id": row.question_id,
            "analysis_type": row.analysis_type,
            "title": row.title,
            "summary_text": row.summary_text,
            "content_json": _parse_json_object(row.content_json),
            "quote_ids": _parse_json_list(row.quote_ids),
            "source_segment_ids": _parse_json_list(row.source_segment_ids),
            "prompt_version": row.prompt_version,
            "input_hash": row.input_hash,
            "model_used": row.model_used,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    if require_trace:
        result = [row for row in result if _has_required_trace(row)]
    return result
