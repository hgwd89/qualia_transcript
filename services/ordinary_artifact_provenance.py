from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from models import db
from models.generated_file import GeneratedFile
from models.interview import Interview
from models.project import Project


ORDINARY_ARTIFACT_TYPES = {"verbatim", "formatted_sheet", "analysis"}
ORDINARY_PROVENANCE_KEY = "source_provenance"
ORDINARY_PROVENANCE_VERSION = "ordinary-artifact-input-v1"


@dataclass(frozen=True)
class OrdinaryArtifactCurrentness:
    current: bool
    provenance_present: bool
    reason: str = ""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(manifest: dict) -> str:
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _date(value) -> str | None:
    return value.isoformat() if value else None


def _participant_manifest(participant, *, include_attributes: bool) -> dict | None:
    if participant is None:
        return None
    payload = {
        "id": int(participant.id),
        "participant_code": str(participant.participant_code or ""),
        "display_name": str(participant.display_name or ""),
    }
    if include_attributes:
        payload["attributes"] = [
            {
                "id": int(attr.id),
                "key": str(attr.attribute_key or ""),
                "value": str(attr.attribute_value or ""),
                "display_order": int(attr.display_order or 0),
            }
            for attr in sorted(
                participant.attributes,
                key=lambda row: (int(row.display_order or 0), int(row.id or 0)),
            )
        ]
    return payload


def _verbatim_referenced_participants(interview) -> list[dict]:
    """Fingerprint every participant identity that can become a rendered speaker name."""
    by_id = {}
    candidates = [interview.participant]
    candidates.extend(segment.participant for segment in interview.segments)
    candidates.extend(assignment.participant for assignment in interview.speaker_assignments)
    for participant in candidates:
        if participant is None:
            continue
        by_id[int(participant.id)] = participant
    return [
        _participant_manifest(by_id[participant_id], include_attributes=False)
        for participant_id in sorted(by_id)
    ]


def _speaker_assignment_manifest(interview) -> list[dict]:
    return [
        {
            "id": int(row.id),
            "speaker_label": str(row.speaker_label or ""),
            "speaker_role": str(row.speaker_role or ""),
            "participant_id": int(row.participant_id) if row.participant_id is not None else None,
        }
        for row in sorted(
            interview.speaker_assignments,
            key=lambda item: (str(item.speaker_label or ""), int(item.id or 0)),
        )
    ]


def _segment_manifest(segment, *, include_mappings: bool, include_flags: bool) -> dict:
    payload = {
        "id": int(segment.id),
        "participant_id": int(segment.participant_id) if segment.participant_id is not None else None,
        "speaker_label": str(segment.speaker_label or ""),
        "speaker_role": str(segment.speaker_role or ""),
        "start_sec": float(segment.start_sec) if segment.start_sec is not None else None,
        "end_sec": float(segment.end_sec) if segment.end_sec is not None else None,
        "text": str(segment.text or ""),
        "seq": int(segment.seq),
    }
    if include_mappings:
        payload["mappings"] = [
            {
                "id": int(mapping.id),
                "question_id": int(mapping.question_id) if mapping.question_id is not None else None,
                "mapped_by": str(mapping.mapped_by or ""),
                "confidence": float(mapping.confidence) if mapping.confidence is not None else None,
                "is_unclassified": bool(mapping.is_unclassified),
            }
            for mapping in sorted(
                segment.utterance_mappings,
                key=lambda row: int(row.id or 0),
            )
        ]
    if include_flags:
        payload["flags"] = [
            {
                "id": int(flag.id),
                "flag_type": str(flag.flag_type or ""),
            }
            for flag in sorted(
                segment.segment_flags,
                key=lambda row: (str(row.flag_type or ""), int(row.id or 0)),
            )
        ]
    return payload


def _flow_manifest(project) -> list[dict]:
    flows = []
    for flow in sorted(project.interview_flows, key=lambda row: int(row.id or 0)):
        sections = []
        for section in sorted(flow.sections, key=lambda row: (int(row.seq), int(row.id))):
            questions = []
            for question in sorted(section.questions, key=lambda row: (int(row.seq), int(row.id))):
                questions.append({
                    "id": int(question.id),
                    "question_code": str(question.question_code or ""),
                    "question_text": str(question.question_text or ""),
                    "question_type": str(question.question_type or ""),
                    "is_key_question": bool(question.is_key_question),
                    "seq": int(question.seq),
                })
            sections.append({
                "id": int(section.id),
                "title": str(section.title or ""),
                "seq": int(section.seq),
                "questions": questions,
            })
        flows.append({
            "id": int(flow.id),
            "title": str(flow.title or ""),
            "version": str(flow.version or ""),
            "sections": sections,
        })
    return flows


def _interview_manifest(
    interview,
    *,
    include_attributes: bool,
    include_mappings: bool,
    include_flags: bool,
    include_assignments: bool,
) -> dict:
    payload = {
        "id": int(interview.id),
        "participant_id": int(interview.participant_id) if interview.participant_id is not None else None,
        "flow_id": int(interview.flow_id) if interview.flow_id is not None else None,
        "interview_date": _date(interview.interview_date),
        "interviewer_name": str(interview.interviewer_name or ""),
        "participant": _participant_manifest(
            interview.participant,
            include_attributes=include_attributes,
        ),
        "segments": [
            _segment_manifest(
                segment,
                include_mappings=include_mappings,
                include_flags=include_flags,
            )
            for segment in sorted(
                interview.segments,
                key=lambda row: (int(row.seq), int(row.id or 0)),
            )
        ],
    }
    if include_assignments:
        payload["speaker_assignments"] = _speaker_assignment_manifest(interview)
    return payload


def build_ordinary_artifact_source_manifest(
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    file_type = str(file_type or "")
    if file_type not in ORDINARY_ARTIFACT_TYPES:
        raise ValueError(f"unsupported ordinary artifact type: {file_type}")

    project = db.session.get(Project, int(project_id))
    if project is None:
        raise ValueError("ordinary artifact project is missing")

    base = {
        "version": ORDINARY_PROVENANCE_VERSION,
        "file_type": file_type,
        "project_id": int(project.id),
        "project_name": str(project.name or ""),
    }

    if file_type == "verbatim":
        if interview_id is None:
            raise ValueError("verbatim provenance requires interview_id")
        interview = db.session.get(Interview, int(interview_id))
        if interview is None or int(interview.project_id) != int(project.id):
            raise ValueError("verbatim interview is missing or cross-project")
        base["project_client"] = str(project.client or "")
        base["interview"] = _interview_manifest(
            interview,
            include_attributes=False,
            include_mappings=False,
            include_flags=True,
            include_assignments=True,
        )
        base["rendered_speaker_participants"] = _verbatim_referenced_participants(interview)
        return base

    interviews = sorted(project.interviews, key=lambda row: int(row.id or 0))
    base["flows"] = _flow_manifest(project)
    if file_type == "formatted_sheet":
        base["interviews"] = [
            _interview_manifest(
                interview,
                include_attributes=False,
                include_mappings=True,
                include_flags=True,
                include_assignments=True,
            )
            for interview in interviews
        ]
        return base

    base["interviews"] = [
        _interview_manifest(
            interview,
            include_attributes=True,
            include_mappings=True,
            include_flags=False,
            include_assignments=False,
        )
        for interview in interviews
    ]
    return base


def capture_ordinary_artifact_source_provenance(
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    manifest = build_ordinary_artifact_source_manifest(
        file_type,
        project_id,
        interview_id=interview_id,
    )
    return {
        "version": ORDINARY_PROVENANCE_VERSION,
        "file_type": str(file_type),
        "project_id": int(project_id),
        "interview_id": int(interview_id) if interview_id is not None else None,
        "sha256": _fingerprint(manifest),
    }


def begin_ordinary_artifact_source_snapshot(
    project_id: int,
    *,
    interview_id: int | None = None,
) -> None:
    """Hold one serialized SQLite source generation through artifact registration."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("ordinary artifact snapshot requires a clean database session")
    if db.engine.dialect.name != "sqlite":
        raise RuntimeError("ordinary artifact serialized source snapshots currently require SQLite")

    db.session.rollback()
    db.session.execute(text("BEGIN IMMEDIATE"))
    project = db.session.get(Project, int(project_id))
    if project is None:
        db.session.rollback()
        raise ValueError("ordinary artifact project is missing")
    if interview_id is not None:
        interview = db.session.get(Interview, int(interview_id))
        if interview is None or int(interview.project_id) != int(project_id):
            db.session.rollback()
            raise ValueError("ordinary artifact interview is missing or cross-project")


def ordinary_artifact_generation_params(
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> str:
    provenance = capture_ordinary_artifact_source_provenance(
        file_type,
        project_id,
        interview_id=interview_id,
    )
    return json.dumps(
        {ORDINARY_PROVENANCE_KEY: provenance},
        ensure_ascii=False,
        sort_keys=True,
    )


def _generation_params(generated_file: GeneratedFile) -> dict:
    raw = generated_file.generation_params_json
    if not raw:
        return {}
    try:
        params = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("generated-file generation metadata is invalid") from exc
    if not isinstance(params, dict):
        raise ValueError("generated-file generation metadata is invalid")
    return params


def ordinary_artifact_currentness(
    generated_file: GeneratedFile,
) -> OrdinaryArtifactCurrentness:
    if generated_file.file_type not in ORDINARY_ARTIFACT_TYPES:
        return OrdinaryArtifactCurrentness(True, False, "")

    try:
        params = _generation_params(generated_file)
    except ValueError as exc:
        return OrdinaryArtifactCurrentness(False, False, str(exc))
    provenance_declared = ORDINARY_PROVENANCE_KEY in params
    expected = params.get(ORDINARY_PROVENANCE_KEY)
    if not isinstance(expected, dict):
        return OrdinaryArtifactCurrentness(
            False,
            provenance_declared,
            (
                "ordinary artifact source provenance is invalid"
                if provenance_declared
                else "ordinary artifact source provenance is missing"
            ),
        )
    if expected.get("version") != ORDINARY_PROVENANCE_VERSION:
        return OrdinaryArtifactCurrentness(
            False,
            True,
            "ordinary artifact source provenance version is missing or unsupported",
        )

    expected_hash = str(expected.get("sha256") or "")
    if not expected_hash:
        return OrdinaryArtifactCurrentness(
            False,
            True,
            "ordinary artifact source provenance hash is missing",
        )
    if str(expected.get("file_type") or "") != str(generated_file.file_type):
        return OrdinaryArtifactCurrentness(False, True, "ordinary artifact file_type provenance mismatch")
    try:
        expected_project_id = int(expected.get("project_id"))
    except (TypeError, ValueError):
        return OrdinaryArtifactCurrentness(False, True, "ordinary artifact project provenance is invalid")
    if expected_project_id != int(generated_file.project_id):
        return OrdinaryArtifactCurrentness(False, True, "ordinary artifact project provenance mismatch")

    expected_interview = expected.get("interview_id")
    current_interview = generated_file.interview_id
    try:
        normalized_expected_interview = (
            int(expected_interview) if expected_interview is not None else None
        )
    except (TypeError, ValueError):
        return OrdinaryArtifactCurrentness(False, True, "ordinary artifact interview provenance is invalid")
    normalized_current_interview = int(current_interview) if current_interview is not None else None
    if normalized_expected_interview != normalized_current_interview:
        return OrdinaryArtifactCurrentness(False, True, "ordinary artifact interview provenance mismatch")

    try:
        current = capture_ordinary_artifact_source_provenance(
            str(generated_file.file_type),
            int(generated_file.project_id),
            interview_id=normalized_current_interview,
        )
    except ValueError as exc:
        return OrdinaryArtifactCurrentness(False, True, str(exc))
    if current["sha256"] != expected_hash:
        return OrdinaryArtifactCurrentness(
            False,
            True,
            "canonical ordinary-artifact inputs changed after generation",
        )
    return OrdinaryArtifactCurrentness(True, True, "")
