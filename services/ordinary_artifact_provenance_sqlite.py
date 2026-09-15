"""Read-only SQLite mirror of ordinary generated-artifact source provenance."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any


ORDINARY_ARTIFACT_TYPES = {"verbatim", "formatted_sheet", "analysis"}
ORDINARY_PROVENANCE_KEY = "source_provenance"
ORDINARY_PROVENANCE_VERSION = "ordinary-artifact-input-v1"


@dataclass(frozen=True)
class SQLiteOrdinaryArtifactCurrentness:
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


def _project_row(con: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = con.execute(
        "SELECT id, name, client FROM projects WHERE id=?",
        (int(project_id),),
    ).fetchone()
    if row is None:
        raise ValueError("ordinary artifact project is missing")
    return row


def _participant_manifest(
    con: sqlite3.Connection,
    participant_id: int | None,
    *,
    include_attributes: bool,
) -> dict | None:
    if participant_id is None:
        return None
    row = con.execute(
        "SELECT id, participant_code, display_name FROM participants WHERE id=?",
        (int(participant_id),),
    ).fetchone()
    if row is None:
        return None
    payload = {
        "id": int(row["id"]),
        "participant_code": str(row["participant_code"] or ""),
        "display_name": str(row["display_name"] or ""),
    }
    if include_attributes:
        attrs = con.execute(
            """
            SELECT id, attribute_key, attribute_value, display_order
            FROM participant_attributes
            WHERE participant_id=?
            ORDER BY display_order, id
            """,
            (int(participant_id),),
        ).fetchall()
        payload["attributes"] = [
            {
                "id": int(attr["id"]),
                "key": str(attr["attribute_key"] or ""),
                "value": str(attr["attribute_value"] or ""),
                "display_order": int(attr["display_order"] or 0),
            }
            for attr in attrs
        ]
    return payload


def _verbatim_referenced_participants(
    con: sqlite3.Connection,
    interview_id: int,
) -> list[dict]:
    rows = con.execute(
        """
        SELECT p.id, p.participant_code, p.display_name
        FROM participants p
        WHERE p.id IN (
            SELECT participant_id FROM interviews
            WHERE id=? AND participant_id IS NOT NULL
            UNION
            SELECT participant_id FROM segments
            WHERE interview_id=? AND participant_id IS NOT NULL
            UNION
            SELECT participant_id FROM speaker_assignments
            WHERE interview_id=? AND participant_id IS NOT NULL
        )
        ORDER BY p.id
        """,
        (int(interview_id), int(interview_id), int(interview_id)),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "participant_code": str(row["participant_code"] or ""),
            "display_name": str(row["display_name"] or ""),
        }
        for row in rows
    ]


def _speaker_assignment_manifest(
    con: sqlite3.Connection,
    interview_id: int,
) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, speaker_label, speaker_role, participant_id
        FROM speaker_assignments
        WHERE interview_id=?
        ORDER BY speaker_label, id
        """,
        (int(interview_id),),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "speaker_label": str(row["speaker_label"] or ""),
            "speaker_role": str(row["speaker_role"] or ""),
            "participant_id": int(row["participant_id"]) if row["participant_id"] is not None else None,
        }
        for row in rows
    ]


def _segment_manifest(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_mappings: bool,
    include_flags: bool,
) -> dict:
    segment_id = int(row["id"])
    payload = {
        "id": segment_id,
        "participant_id": int(row["participant_id"]) if row["participant_id"] is not None else None,
        "speaker_label": str(row["speaker_label"] or ""),
        "speaker_role": str(row["speaker_role"] or ""),
        "start_sec": float(row["start_sec"]) if row["start_sec"] is not None else None,
        "end_sec": float(row["end_sec"]) if row["end_sec"] is not None else None,
        "text": str(row["text"] or ""),
        "seq": int(row["seq"]),
    }
    if include_mappings:
        mappings = con.execute(
            """
            SELECT id, question_id, mapped_by, confidence, is_unclassified
            FROM utterance_mappings
            WHERE segment_id=?
            ORDER BY id
            """,
            (segment_id,),
        ).fetchall()
        payload["mappings"] = [
            {
                "id": int(mapping["id"]),
                "question_id": int(mapping["question_id"]) if mapping["question_id"] is not None else None,
                "mapped_by": str(mapping["mapped_by"] or ""),
                "confidence": float(mapping["confidence"]) if mapping["confidence"] is not None else None,
                "is_unclassified": bool(mapping["is_unclassified"]),
            }
            for mapping in mappings
        ]
    if include_flags:
        flags = con.execute(
            """
            SELECT id, flag_type
            FROM segment_flags
            WHERE segment_id=?
            ORDER BY flag_type, id
            """,
            (segment_id,),
        ).fetchall()
        payload["flags"] = [
            {
                "id": int(flag["id"]),
                "flag_type": str(flag["flag_type"] or ""),
            }
            for flag in flags
        ]
    return payload


def _flow_manifest(con: sqlite3.Connection, project_id: int) -> list[dict]:
    flows = con.execute(
        "SELECT id, title, version FROM interview_flows WHERE project_id=? ORDER BY id",
        (int(project_id),),
    ).fetchall()
    result: list[dict] = []
    for flow in flows:
        sections = con.execute(
            """
            SELECT id, title, seq
            FROM interview_flow_sections
            WHERE flow_id=?
            ORDER BY seq, id
            """,
            (int(flow["id"]),),
        ).fetchall()
        section_items: list[dict] = []
        for section in sections:
            questions = con.execute(
                """
                SELECT id, question_code, question_text, question_type,
                       is_key_question, seq
                FROM interview_flow_questions
                WHERE section_id=?
                ORDER BY seq, id
                """,
                (int(section["id"]),),
            ).fetchall()
            section_items.append({
                "id": int(section["id"]),
                "title": str(section["title"] or ""),
                "seq": int(section["seq"]),
                "questions": [
                    {
                        "id": int(question["id"]),
                        "question_code": str(question["question_code"] or ""),
                        "question_text": str(question["question_text"] or ""),
                        "question_type": str(question["question_type"] or ""),
                        "is_key_question": bool(question["is_key_question"]),
                        "seq": int(question["seq"]),
                    }
                    for question in questions
                ],
            })
        result.append({
            "id": int(flow["id"]),
            "title": str(flow["title"] or ""),
            "version": str(flow["version"] or ""),
            "sections": section_items,
        })
    return result


def _interview_manifest(
    con: sqlite3.Connection,
    interview_id: int,
    *,
    include_attributes: bool,
    include_mappings: bool,
    include_flags: bool,
    include_assignments: bool,
) -> dict:
    row = con.execute(
        """
        SELECT id, project_id, participant_id, flow_id, interview_date,
               interviewer_name
        FROM interviews
        WHERE id=?
        """,
        (int(interview_id),),
    ).fetchone()
    if row is None:
        raise ValueError("ordinary artifact interview is missing")
    segments = con.execute(
        """
        SELECT id, participant_id, speaker_label, speaker_role, start_sec,
               end_sec, text, seq
        FROM segments
        WHERE interview_id=?
        ORDER BY seq, id
        """,
        (int(interview_id),),
    ).fetchall()
    payload = {
        "id": int(row["id"]),
        "participant_id": int(row["participant_id"]) if row["participant_id"] is not None else None,
        "flow_id": int(row["flow_id"]) if row["flow_id"] is not None else None,
        "interview_date": str(row["interview_date"]) if row["interview_date"] is not None else None,
        "interviewer_name": str(row["interviewer_name"] or ""),
        "participant": _participant_manifest(
            con,
            int(row["participant_id"]) if row["participant_id"] is not None else None,
            include_attributes=include_attributes,
        ),
        "segments": [
            _segment_manifest(
                con,
                segment,
                include_mappings=include_mappings,
                include_flags=include_flags,
            )
            for segment in segments
        ],
    }
    if include_assignments:
        payload["speaker_assignments"] = _speaker_assignment_manifest(con, interview_id)
    return payload


def build_ordinary_artifact_source_manifest_sqlite(
    con: sqlite3.Connection,
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    file_type = str(file_type or "")
    if file_type not in ORDINARY_ARTIFACT_TYPES:
        raise ValueError(f"unsupported ordinary artifact type: {file_type}")
    project = _project_row(con, project_id)
    base = {
        "version": ORDINARY_PROVENANCE_VERSION,
        "file_type": file_type,
        "project_id": int(project["id"]),
        "project_name": str(project["name"] or ""),
    }

    if file_type == "verbatim":
        if interview_id is None:
            raise ValueError("verbatim provenance requires interview_id")
        interview_project = con.execute(
            "SELECT project_id FROM interviews WHERE id=?",
            (int(interview_id),),
        ).fetchone()
        if interview_project is None or int(interview_project["project_id"]) != int(project_id):
            raise ValueError("verbatim interview is missing or cross-project")
        base["project_client"] = str(project["client"] or "")
        base["interview"] = _interview_manifest(
            con,
            int(interview_id),
            include_attributes=False,
            include_mappings=False,
            include_flags=True,
            include_assignments=True,
        )
        base["rendered_speaker_participants"] = _verbatim_referenced_participants(
            con,
            int(interview_id),
        )
        return base

    interviews = con.execute(
        "SELECT id FROM interviews WHERE project_id=? ORDER BY id",
        (int(project_id),),
    ).fetchall()
    base["flows"] = _flow_manifest(con, project_id)
    if file_type == "formatted_sheet":
        base["interviews"] = [
            _interview_manifest(
                con,
                int(interview["id"]),
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
            con,
            int(interview["id"]),
            include_attributes=True,
            include_mappings=True,
            include_flags=False,
            include_assignments=False,
        )
        for interview in interviews
    ]
    return base


def capture_ordinary_artifact_source_provenance_sqlite(
    con: sqlite3.Connection,
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    manifest = build_ordinary_artifact_source_manifest_sqlite(
        con,
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


def ordinary_artifact_currentness_sqlite(
    con: sqlite3.Connection,
    *,
    file_type: str,
    project_id: int,
    interview_id: int | None,
    generation_params_json: str | None,
) -> SQLiteOrdinaryArtifactCurrentness:
    if file_type not in ORDINARY_ARTIFACT_TYPES:
        return SQLiteOrdinaryArtifactCurrentness(True, False, "")
    if not generation_params_json:
        return SQLiteOrdinaryArtifactCurrentness(
            False,
            False,
            "ordinary artifact source provenance is missing",
        )
    try:
        params = json.loads(generation_params_json)
    except (json.JSONDecodeError, TypeError):
        return SQLiteOrdinaryArtifactCurrentness(False, False, "generated-file generation metadata is invalid")
    if not isinstance(params, dict):
        return SQLiteOrdinaryArtifactCurrentness(False, False, "generated-file generation metadata is invalid")
    provenance_declared = ORDINARY_PROVENANCE_KEY in params
    expected = params.get(ORDINARY_PROVENANCE_KEY)
    if not isinstance(expected, dict):
        return SQLiteOrdinaryArtifactCurrentness(
            False,
            provenance_declared,
            (
                "ordinary artifact source provenance is invalid"
                if provenance_declared
                else "ordinary artifact source provenance is missing"
            ),
        )
    if expected.get("version") != ORDINARY_PROVENANCE_VERSION:
        return SQLiteOrdinaryArtifactCurrentness(False, True, "ordinary artifact source provenance version is missing or unsupported")
    expected_hash = str(expected.get("sha256") or "")
    if not expected_hash:
        return SQLiteOrdinaryArtifactCurrentness(False, True, "ordinary artifact source provenance hash is missing")
    if str(expected.get("file_type") or "") != str(file_type):
        return SQLiteOrdinaryArtifactCurrentness(False, True, "ordinary artifact file_type provenance mismatch")
    try:
        expected_project_id = int(expected.get("project_id"))
    except (TypeError, ValueError):
        return SQLiteOrdinaryArtifactCurrentness(False, True, "ordinary artifact project provenance is invalid")
    if expected_project_id != int(project_id):
        return SQLiteOrdinaryArtifactCurrentness(False, True, "ordinary artifact project provenance mismatch")

    expected_interview = expected.get("interview_id")
    try:
        normalized_expected_interview = int(expected_interview) if expected_interview is not None else None
    except (TypeError, ValueError):
        return SQLiteOrdinaryArtifactCurrentness(False, True, "ordinary artifact interview provenance is invalid")
    normalized_interview = int(interview_id) if interview_id is not None else None
    if normalized_expected_interview != normalized_interview:
        return SQLiteOrdinaryArtifactCurrentness(False, True, "ordinary artifact interview provenance mismatch")

    try:
        current = capture_ordinary_artifact_source_provenance_sqlite(
            con,
            file_type,
            project_id,
            interview_id=normalized_interview,
        )
    except ValueError as exc:
        return SQLiteOrdinaryArtifactCurrentness(False, True, str(exc))
    if current["sha256"] != expected_hash:
        return SQLiteOrdinaryArtifactCurrentness(
            False,
            True,
            "canonical ordinary-artifact inputs changed after generation",
        )
    return SQLiteOrdinaryArtifactCurrentness(True, True, "")
