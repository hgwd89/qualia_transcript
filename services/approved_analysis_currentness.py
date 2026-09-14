"""Currentness checks for formal approved-analysis outputs.

Formal approved-analysis files are immutable historical artifacts, but the normal
professional-delivery path must only expose one as current when it still
represents the complete set of currently approved analyses and every included
analysis still proves the same canonical source provenance recorded at export.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from models import db
from models.analysis import AIAnalysis
from models.generated_file import GeneratedFile
from services.analysis_source_provenance import (
    PROVENANCE_KEY,
    analysis_source_provenance_status,
)


@dataclass(frozen=True)
class CurrentnessStatus:
    current: bool
    reason: str = ""


def _analysis_content(analysis: AIAnalysis) -> dict:
    try:
        value = json.loads(analysis.content_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def formal_approved_analysis_readiness(project_id: int) -> dict:
    """Return advisory UI readiness for generating a new formal workbook.

    The report writer remains the authoritative serialized gate. This helper is
    intentionally read-only and exists so the UI does not call stale analyses
    deliverable merely because their persisted review_status is still approved.
    """
    analyses = (
        AIAnalysis.query
        .filter_by(project_id=int(project_id), review_status="approved")
        .order_by(AIAnalysis.id.asc())
        .all()
    )
    current_ids: list[int] = []
    invalid: list[dict] = []
    for analysis in analyses:
        ok, reason = analysis_source_provenance_status(analysis)
        if ok:
            current_ids.append(int(analysis.id))
        else:
            invalid.append({"analysis_id": int(analysis.id), "reason": str(reason)})
    return {
        "approved_count": len(analyses),
        "current_count": len(current_ids),
        "invalid_count": len(invalid),
        "current_analysis_ids": current_ids,
        "invalid": invalid,
        "ready": bool(analyses) and not invalid,
    }


def approved_analysis_artifact_currentness(generated_file: GeneratedFile) -> CurrentnessStatus:
    """Prove that an approved-analysis artifact is still the current formal set.

    Missing legacy metadata fails closed. The file itself is never deleted; this
    function only decides whether the ordinary formal-delivery path may expose it.
    """
    if generated_file.file_type != "approved_analysis":
        return CurrentnessStatus(True, "")

    try:
        params = json.loads(generated_file.generation_params_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return CurrentnessStatus(False, "formal artifact generation metadata is invalid")
    if not isinstance(params, dict) or params.get("approved_only") is not True:
        return CurrentnessStatus(False, "formal artifact generation provenance is missing")

    raw_ids = params.get("analysis_ids")
    hashes = params.get("source_provenance_sha256")
    if not isinstance(raw_ids, list) or not raw_ids or not isinstance(hashes, dict):
        return CurrentnessStatus(False, "formal artifact analysis provenance is missing")
    try:
        analysis_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return CurrentnessStatus(False, "formal artifact analysis IDs are invalid")
    if len(set(analysis_ids)) != len(analysis_ids):
        return CurrentnessStatus(False, "formal artifact analysis IDs are duplicated")
    if params.get("analysis_count") is not None:
        try:
            if int(params.get("analysis_count")) != len(analysis_ids):
                return CurrentnessStatus(False, "formal artifact analysis count is inconsistent")
        except (TypeError, ValueError):
            return CurrentnessStatus(False, "formal artifact analysis count is invalid")

    current_approved_ids = [
        int(row.id)
        for row in (
            AIAnalysis.query
            .filter_by(project_id=int(generated_file.project_id), review_status="approved")
            .order_by(AIAnalysis.id.asc())
            .all()
        )
    ]
    if sorted(analysis_ids) != current_approved_ids:
        return CurrentnessStatus(False, "formal artifact no longer matches the current approved analysis set")

    for analysis_id in analysis_ids:
        analysis = db.session.get(AIAnalysis, int(analysis_id))
        if analysis is None or int(analysis.project_id or -1) != int(generated_file.project_id):
            return CurrentnessStatus(False, f"formal artifact analysis id={analysis_id} is missing or cross-project")
        if analysis.review_status != "approved":
            return CurrentnessStatus(False, f"formal artifact analysis id={analysis_id} is no longer approved")

        content = _analysis_content(analysis)
        provenance = content.get(PROVENANCE_KEY) if content else None
        stored_hash = str((provenance or {}).get("sha256") or "") if isinstance(provenance, dict) else ""
        artifact_hash = str(hashes.get(str(int(analysis_id))) or "")
        if not stored_hash or artifact_hash != stored_hash:
            return CurrentnessStatus(False, f"formal artifact analysis id={analysis_id} does not match its exported source hash")

        ok, reason = analysis_source_provenance_status(analysis)
        if not ok:
            return CurrentnessStatus(False, f"formal artifact analysis id={analysis_id} source is stale or unprovable: {reason}")

    return CurrentnessStatus(True, "")
