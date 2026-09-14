"""Currentness checks for formal approved-analysis outputs.

Formal approved-analysis files are immutable generated history, but the normal
professional-delivery path must only expose one as current when it still
represents the complete set of currently approved analyses, the exact analysis
state exported into the workbook, and the canonical source provenance recorded
at export. Byte integrity is separately verified from the registered artifact
SHA-256 before download bytes are exposed.
"""
from __future__ import annotations

import hashlib
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
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"AIAnalysis id={analysis.id} content_json is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"AIAnalysis id={analysis.id} content_json is not an object")
    return value


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def formal_analysis_state_sha256(analysis: AIAnalysis) -> str:
    """Hash the exact analysis/review fields represented by the formal workbook."""
    content = _analysis_content(analysis)
    findings = content.get("findings") or []
    if not isinstance(findings, list):
        raise ValueError(f"AIAnalysis id={analysis.id} findings is not an array")

    normalized_findings = []
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            raise ValueError(f"AIAnalysis id={analysis.id} finding #{index} is not an object")
        normalized_findings.append({
            "point": finding.get("point", "") or "",
            "evidence_quote": finding.get("evidence_quote", "") or "",
            "source_segment_ids": list(finding.get("source_segment_ids") or []),
            "participant_codes": list(finding.get("participant_codes") or []),
            "question_codes": list(finding.get("question_codes") or []),
            "confidence": finding.get("confidence", "") or "",
        })

    participant_code = ""
    if analysis.interview and analysis.interview.participant:
        participant_code = analysis.interview.participant.participant_code or ""
    question_code = analysis.question.question_code or "" if analysis.question else ""

    state = {
        "analysis_id": int(analysis.id),
        "analysis_type": str(analysis.analysis_type or ""),
        "title": analysis.title or "",
        "participant_code": participant_code,
        "question_code": question_code,
        "summary_text": analysis.summary_text or "",
        "implications": content.get("implications", "") or "",
        "unresolved": content.get("unresolved", "") or "",
        "review_status": analysis.review_status or "",
        "review_note": analysis.review_note or "",
        "reviewed_at": _iso(analysis.reviewed_at),
        "model_used": analysis.model_used or "",
        "created_at": _iso(analysis.created_at),
        "findings": normalized_findings,
    }
    return hashlib.sha256(_canonical_json(state).encode("utf-8")).hexdigest()


def _generation_params(generated_file: GeneratedFile) -> dict:
    try:
        params = json.loads(generated_file.generation_params_json or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("formal artifact generation metadata is invalid") from exc
    if not isinstance(params, dict):
        raise ValueError("formal artifact generation metadata is invalid")
    return params


def formal_artifact_expected_sha256(generated_file: GeneratedFile) -> str:
    """Return the registered formal artifact hash or fail closed."""
    params = _generation_params(generated_file)
    value = str(params.get("artifact_sha256") or "").strip().lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("formal artifact SHA-256 metadata is missing or invalid")
    return value


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
    The actual file bytes are verified against ``artifact_sha256`` by the download
    boundary immediately before constructing the immutable response snapshot.
    """
    if generated_file.file_type != "approved_analysis":
        return CurrentnessStatus(True, "")

    try:
        params = _generation_params(generated_file)
        formal_artifact_expected_sha256(generated_file)
    except ValueError as exc:
        return CurrentnessStatus(False, str(exc))
    if params.get("approved_only") is not True:
        return CurrentnessStatus(False, "formal artifact generation provenance is missing")

    raw_ids = params.get("analysis_ids")
    hashes = params.get("source_provenance_sha256")
    state_hashes = params.get("formal_analysis_state_sha256")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or not isinstance(hashes, dict)
        or not isinstance(state_hashes, dict)
    ):
        return CurrentnessStatus(False, "formal artifact analysis provenance/state metadata is missing")
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

        try:
            content = _analysis_content(analysis)
        except ValueError as exc:
            return CurrentnessStatus(False, str(exc))
        provenance = content.get(PROVENANCE_KEY)
        stored_hash = str((provenance or {}).get("sha256") or "") if isinstance(provenance, dict) else ""
        artifact_hash = str(hashes.get(str(int(analysis_id))) or "")
        if not stored_hash or artifact_hash != stored_hash:
            return CurrentnessStatus(False, f"formal artifact analysis id={analysis_id} does not match its exported source hash")

        try:
            current_state_hash = formal_analysis_state_sha256(analysis)
        except ValueError as exc:
            return CurrentnessStatus(False, str(exc))
        artifact_state_hash = str(state_hashes.get(str(int(analysis_id))) or "")
        if not artifact_state_hash or artifact_state_hash != current_state_hash:
            return CurrentnessStatus(False, f"formal artifact analysis id={analysis_id} no longer matches its exported analysis/review state")

        ok, reason = analysis_source_provenance_status(analysis)
        if not ok:
            return CurrentnessStatus(False, f"formal artifact analysis id={analysis_id} source is stale or unprovable: {reason}")

    return CurrentnessStatus(True, "")
