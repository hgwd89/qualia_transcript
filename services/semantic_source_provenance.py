"""Long-lived source provenance for persisted semantic-cluster analyses.

Semantic clustering reads a deterministic prefix of respondent Segment rows and
then derives fragments, embeddings, clusters, and optional AI summaries from
those rows. Durable job fencing prevents supported UI writes while the worker is
active; this module adds the complementary persisted proof needed after the job
finishes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from models import db
from models.analysis import AIAnalysis
from models.interview import Interview
from models.segment import Segment

SEMANTIC_PROVENANCE_KEY = "source_provenance"
SEMANTIC_REQUEST_KEY = "semantic_request"
SEMANTIC_PROVENANCE_VERSION = "semantic-input-v1"


class SemanticSourceProvenanceError(ValueError):
    """The source state needed to validate semantic analysis cannot be proven."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(manifest: dict) -> str:
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _normalize_max_segments(value: int | None) -> int | None:
    if value is None:
        return None
    normalized = int(value)
    if normalized <= 0:
        raise SemanticSourceProvenanceError("semantic max_segments must be positive")
    return normalized


def _segment_manifest(segment: Segment) -> dict:
    # These are exactly the canonical fields consumed by fragmentation and the
    # semantic payload. Keep this list explicit so future input expansion must
    # update the provenance contract deliberately.
    return {
        "id": int(segment.id),
        "seq": int(segment.seq),
        "text": str(segment.text or ""),
        "speaker_label": str(segment.speaker_label or ""),
        "speaker_role": str(segment.speaker_role or ""),
        "participant_id": int(segment.participant_id) if segment.participant_id is not None else None,
        "start_sec": float(segment.start_sec) if segment.start_sec is not None else None,
        "end_sec": float(segment.end_sec) if segment.end_sec is not None else None,
    }


def _candidate_segments(interview_id: int, max_segments: int | None) -> list[Segment]:
    rows = (
        Segment.query
        .filter_by(interview_id=int(interview_id), speaker_role="respondent")
        .order_by(Segment.seq.asc(), Segment.id.asc())
        .all()
    )
    if max_segments is not None:
        rows = rows[:max_segments]
    return rows


def build_semantic_source_manifest(
    project_id: int,
    interview_id: int,
    *,
    max_segments: int | None,
    no_ai: bool,
) -> dict:
    project_id = int(project_id)
    interview_id = int(interview_id)
    max_segments = _normalize_max_segments(max_segments)
    if not isinstance(no_ai, bool):
        raise SemanticSourceProvenanceError("semantic no_ai must be boolean")

    interview = db.session.get(Interview, interview_id)
    if interview is None or int(interview.project_id) != project_id:
        raise SemanticSourceProvenanceError("semantic interview is missing or cross-project")

    return {
        "version": SEMANTIC_PROVENANCE_VERSION,
        "analysis_type": "semantic_clusters",
        "project_id": project_id,
        "interview_id": interview_id,
        "request": {
            "max_segments": max_segments,
            "no_ai": no_ai,
        },
        "candidate_segments": [
            _segment_manifest(segment)
            for segment in _candidate_segments(interview_id, max_segments)
        ],
    }


def capture_semantic_source_provenance(
    project_id: int,
    interview_id: int,
    *,
    max_segments: int | None,
    no_ai: bool,
) -> dict:
    manifest = build_semantic_source_manifest(
        project_id,
        interview_id,
        max_segments=max_segments,
        no_ai=no_ai,
    )
    return {
        "version": SEMANTIC_PROVENANCE_VERSION,
        "sha256": _fingerprint(manifest),
    }


def semantic_source_provenance_matches_scope(
    expected: dict,
    project_id: int,
    interview_id: int,
    *,
    max_segments: int | None,
    no_ai: bool,
) -> tuple[bool, str]:
    if not isinstance(expected, dict):
        return False, "semantic source provenance is missing"
    if expected.get("version") != SEMANTIC_PROVENANCE_VERSION:
        return False, "semantic source provenance version is missing or unsupported"
    expected_hash = str(expected.get("sha256") or "")
    if not expected_hash:
        return False, "semantic source provenance hash is missing"

    try:
        current = capture_semantic_source_provenance(
            project_id,
            interview_id,
            max_segments=max_segments,
            no_ai=no_ai,
        )
    except SemanticSourceProvenanceError as exc:
        return False, str(exc)

    if current["sha256"] != expected_hash:
        return False, "canonical semantic inputs changed after generation"
    return True, ""


def semantic_analysis_source_provenance_status(analysis: AIAnalysis) -> tuple[bool, str]:
    if analysis.analysis_type != "semantic_clusters":
        return False, f"unsupported semantic analysis_type: {analysis.analysis_type}"
    if analysis.interview_id is None:
        return False, "semantic analysis interview_id is missing"

    try:
        content = json.loads(analysis.content_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False, "semantic analysis content_json is invalid"
    if not isinstance(content, dict):
        return False, "semantic analysis content_json is not an object"

    request_payload = content.get(SEMANTIC_REQUEST_KEY)
    if not isinstance(request_payload, dict):
        return False, "semantic request provenance is missing"
    max_segments = request_payload.get("max_segments")
    no_ai = request_payload.get("no_ai")
    try:
        max_segments = _normalize_max_segments(max_segments)
    except (SemanticSourceProvenanceError, TypeError, ValueError) as exc:
        return False, str(exc)
    if not isinstance(no_ai, bool):
        return False, "semantic request no_ai is missing or invalid"

    return semantic_source_provenance_matches_scope(
        content.get(SEMANTIC_PROVENANCE_KEY),
        int(analysis.project_id),
        int(analysis.interview_id),
        max_segments=max_segments,
        no_ai=no_ai,
    )
