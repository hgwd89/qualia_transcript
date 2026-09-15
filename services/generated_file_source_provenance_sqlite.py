"""Read-only SQLite parity for ordinary GeneratedFile source provenance."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from services.generated_file_source_provenance import PROVENANCE_VERSION


class GeneratedFileSourceProvenanceSQLiteError(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(manifest: dict) -> str:
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _one(con: sqlite3.Connection, sql: str, params=()) -> sqlite3.Row:
    row = con.execute(sql, params).fetchone()
    if row is None:
        raise GeneratedFileSourceProvenanceSQLiteError("required source row is missing")
    return row


def _project(con: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    return _one(
        con,
        "SELECT id, name, client FROM main.projects WHERE id=?",
        (int(project_id),),
    )


def _participant_identity(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "participant_code": str(row["participant_code"] or ""),
        "display_name": str(row["display_name"] or ""),
    }


def _interview_identity(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "participant_id": int(row["participant_id"]) if row["participant_id"] is not None else None,
        "flow_id": int(row["flow_id"]) if row["flow_id"] is not None else None,
        "interview_date": str(row["interview_date"]) if row["interview_date"] not in {None, ""} else None,
        "interviewer_name": str(row["interviewer_name"] or ""),
    }


def _segment_core(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "interview_id": int(row["interview_id"]),
        "participant_id": int(row["participant_id"]) if row["participant_id"] is not None else None,
        "speaker_label": str(row["speaker_label"] or ""),
        "speaker_role": str(row["speaker_role"] or ""),
        "seq": int(row["seq"]),
        "start_sec": row["start_sec"],
        "end_sec": row["end_sec"],
        "text": str(row["text"]),
    }


def _interview_rows(con: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id, participant_id, flow_id, interview_date, interviewer_name
        FROM main.interviews
        WHERE project_id=?
        ORDER BY id
        """,
        (int(project_id),),
    ).fetchall()


def _participant_rows(con: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id, participant_code, display_name
        FROM main.participants
        WHERE project_id=?
        ORDER BY id
        """,
        (int(project_id),),
    ).fetchall()


def _verbatim_manifest(con: sqlite3.Connection, project_id: int, interview_id: int | None) -> dict:
    if interview_id is None:
        raise GeneratedFileSourceProvenanceSQLiteError("verbatim requires interview_id")
    project = _project(con, project_id)
    interview = _one(
        con,
        """
        SELECT id, project_id, participant_id, flow_id, interview_date, interviewer_name
        FROM main.interviews WHERE id=?
        """,
        (int(interview_id),),
    )
    if int(interview["project_id"]) != int(project_id):
        raise GeneratedFileSourceProvenanceSQLiteError("verbatim interview is cross-project")

    segments = con.execute(
        """
        SELECT id, interview_id, participant_id, speaker_label, speaker_role,
               seq, start_sec, end_sec, text
        FROM main.segments
        WHERE interview_id=?
        ORDER BY seq, id
        """,
        (int(interview_id),),
    ).fetchall()
    assignments = con.execute(
        """
        SELECT id, interview_id, speaker_label, speaker_role, participant_id
        FROM main.speaker_assignments
        WHERE interview_id=?
        ORDER BY speaker_label, id
        """,
        (int(interview_id),),
    ).fetchall()

    participant_ids = set()
    if interview["participant_id"] is not None:
        participant_ids.add(int(interview["participant_id"]))
    for row in segments:
        if row["participant_id"] is not None:
            participant_ids.add(int(row["participant_id"]))
    for row in assignments:
        if row["participant_id"] is not None:
            participant_ids.add(int(row["participant_id"]))

    participants = []
    if participant_ids:
        placeholders = ",".join("?" for _ in participant_ids)
        participants = con.execute(
            f"""
            SELECT id, participant_code, display_name
            FROM main.participants
            WHERE id IN ({placeholders})
            ORDER BY id
            """,
            tuple(sorted(participant_ids)),
        ).fetchall()

    flags = con.execute(
        """
        SELECT sf.segment_id, sf.flag_type
        FROM main.segment_flags sf
        JOIN main.segments s ON s.id=sf.segment_id
        WHERE s.interview_id=? AND sf.flag_type='quote'
        ORDER BY sf.segment_id, sf.id
        """,
        (int(interview_id),),
    ).fetchall()

    return {
        "version": PROVENANCE_VERSION,
        "file_type": "verbatim",
        "project": {
            "id": int(project["id"]),
            "name": str(project["name"] or ""),
            "client": str(project["client"] or ""),
        },
        "interview": _interview_identity(interview),
        "participants": [_participant_identity(row) for row in participants],
        "speaker_assignments": [
            {
                "id": int(row["id"]),
                "speaker_label": str(row["speaker_label"] or ""),
                "speaker_role": str(row["speaker_role"] or ""),
                "participant_id": int(row["participant_id"]) if row["participant_id"] is not None else None,
            }
            for row in assignments
        ],
        "segments": [_segment_core(row) for row in segments],
        "quote_flags": [
            {"segment_id": int(row["segment_id"]), "flag_type": "quote"}
            for row in flags
        ],
    }


def _formatted_manifest(con: sqlite3.Connection, project_id: int) -> dict:
    project = _project(con, project_id)
    interviews = _interview_rows(con, project_id)
    interview_ids = [int(row["id"]) for row in interviews]
    participants = _participant_rows(con, project_id)

    segments = []
    assignments = []
    if interview_ids:
        placeholders = ",".join("?" for _ in interview_ids)
        segments = con.execute(
            f"""
            SELECT id, interview_id, participant_id, speaker_label, speaker_role,
                   seq, start_sec, end_sec, text
            FROM main.segments
            WHERE interview_id IN ({placeholders})
            ORDER BY interview_id, seq, id
            """,
            tuple(interview_ids),
        ).fetchall()
        assignments = con.execute(
            f"""
            SELECT id, interview_id, speaker_label, speaker_role, participant_id
            FROM main.speaker_assignments
            WHERE interview_id IN ({placeholders})
            ORDER BY interview_id, speaker_label, id
            """,
            tuple(interview_ids),
        ).fetchall()

    segment_ids = [int(row["id"]) for row in segments]
    mappings = []
    flags = []
    if segment_ids:
        placeholders = ",".join("?" for _ in segment_ids)
        mappings = con.execute(
            f"""
            SELECT id, segment_id, question_id, is_unclassified
            FROM main.utterance_mappings
            WHERE segment_id IN ({placeholders})
            ORDER BY segment_id, id
            """,
            tuple(segment_ids),
        ).fetchall()
        flags = con.execute(
            f"""
            SELECT segment_id, flag_type
            FROM main.segment_flags
            WHERE segment_id IN ({placeholders})
              AND flag_type IN ('favorite','quote','exclude','needs_review')
            ORDER BY segment_id, flag_type, id
            """,
            tuple(segment_ids),
        ).fetchall()

    flow_rows = []
    flows = con.execute(
        "SELECT id, title FROM main.interview_flows WHERE project_id=? ORDER BY id",
        (int(project_id),),
    ).fetchall()
    for flow in flows:
        sections = []
        section_rows = con.execute(
            """
            SELECT id, title, seq
            FROM main.interview_flow_sections
            WHERE flow_id=?
            ORDER BY seq, id
            """,
            (int(flow["id"]),),
        ).fetchall()
        for section in section_rows:
            questions = con.execute(
                """
                SELECT id, question_code, question_text, is_key_question, seq
                FROM main.interview_flow_questions
                WHERE section_id=?
                ORDER BY seq, id
                """,
                (int(section["id"]),),
            ).fetchall()
            sections.append({
                "id": int(section["id"]),
                "title": str(section["title"] or ""),
                "seq": int(section["seq"]),
                "questions": [
                    {
                        "id": int(question["id"]),
                        "question_code": str(question["question_code"] or ""),
                        "question_text": str(question["question_text"] or ""),
                        "is_key_question": bool(question["is_key_question"]),
                        "seq": int(question["seq"]),
                    }
                    for question in questions
                ],
            })
        flow_rows.append({
            "id": int(flow["id"]),
            "title": str(flow["title"] or ""),
            "sections": sections,
        })

    return {
        "version": PROVENANCE_VERSION,
        "file_type": "formatted_sheet",
        "project": {"id": int(project["id"]), "name": str(project["name"] or "")},
        "interviews": [_interview_identity(row) for row in interviews],
        "participants": [_participant_identity(row) for row in participants],
        "flows": flow_rows,
        "segments": [_segment_core(row) for row in segments],
        "mappings": [
            {
                "id": int(row["id"]),
                "segment_id": int(row["segment_id"]),
                "question_id": int(row["question_id"]) if row["question_id"] is not None else None,
                "is_unclassified": bool(row["is_unclassified"]),
            }
            for row in mappings
        ],
        "speaker_assignments": [
            {
                "id": int(row["id"]),
                "interview_id": int(row["interview_id"]),
                "speaker_label": str(row["speaker_label"] or ""),
                "speaker_role": str(row["speaker_role"] or ""),
                "participant_id": int(row["participant_id"]) if row["participant_id"] is not None else None,
            }
            for row in assignments
        ],
        "flags": [
            {"segment_id": int(row["segment_id"]), "flag_type": str(row["flag_type"])}
            for row in flags
        ],
    }


def _analysis_manifest(con: sqlite3.Connection, project_id: int) -> dict:
    project = _project(con, project_id)
    interviews = _interview_rows(con, project_id)
    interview_ids = [int(row["id"]) for row in interviews]
    participants = _participant_rows(con, project_id)

    segments = []
    if interview_ids:
        placeholders = ",".join("?" for _ in interview_ids)
        segments = con.execute(
            f"""
            SELECT id, interview_id, participant_id, speaker_label, speaker_role,
                   seq, start_sec, end_sec, text
            FROM main.segments
            WHERE interview_id IN ({placeholders})
            ORDER BY interview_id, seq, id
            """,
            tuple(interview_ids),
        ).fetchall()
    segment_ids = [int(row["id"]) for row in segments]
    mappings = []
    if segment_ids:
        placeholders = ",".join("?" for _ in segment_ids)
        mappings = con.execute(
            f"""
            SELECT id, segment_id, question_id, mapped_by, confidence, is_unclassified
            FROM main.utterance_mappings
            WHERE segment_id IN ({placeholders})
            ORDER BY segment_id, id
            """,
            tuple(segment_ids),
        ).fetchall()

    participant_rows = []
    for participant in participants:
        attributes = con.execute(
            """
            SELECT id, attribute_key, attribute_value, display_order
            FROM main.participant_attributes
            WHERE participant_id=?
            ORDER BY display_order, id
            """,
            (int(participant["id"]),),
        ).fetchall()
        participant_rows.append({
            **_participant_identity(participant),
            "attributes": [
                {
                    "id": int(attribute["id"]),
                    "attribute_key": str(attribute["attribute_key"] or ""),
                    "attribute_value": str(attribute["attribute_value"] or ""),
                    "display_order": int(attribute["display_order"] or 0),
                }
                for attribute in attributes
            ],
        })

    question_ids = sorted({
        int(row["question_id"])
        for row in mappings
        if row["question_id"] is not None
    })
    question_rows = []
    if question_ids:
        placeholders = ",".join("?" for _ in question_ids)
        questions = con.execute(
            f"""
            SELECT q.id, q.section_id, q.question_code, q.question_text,
                   q.is_key_question, sec.title AS section_title
            FROM main.interview_flow_questions q
            LEFT JOIN main.interview_flow_sections sec ON sec.id=q.section_id
            WHERE q.id IN ({placeholders})
            ORDER BY q.id
            """,
            tuple(question_ids),
        ).fetchall()
        question_rows = [
            {
                "id": int(question["id"]),
                "section_id": int(question["section_id"]),
                "section_title": str(question["section_title"] or ""),
                "question_code": str(question["question_code"] or ""),
                "question_text": str(question["question_text"] or ""),
                "is_key_question": bool(question["is_key_question"]),
            }
            for question in questions
        ]

    return {
        "version": PROVENANCE_VERSION,
        "file_type": "analysis",
        "project": {"id": int(project["id"]), "name": str(project["name"] or "")},
        "interviews": [_interview_identity(row) for row in interviews],
        "participants": participant_rows,
        "segments": [_segment_core(row) for row in segments],
        "mappings": [
            {
                "id": int(row["id"]),
                "segment_id": int(row["segment_id"]),
                "question_id": int(row["question_id"]) if row["question_id"] is not None else None,
                "mapped_by": str(row["mapped_by"] or ""),
                "confidence": row["confidence"],
                "is_unclassified": bool(row["is_unclassified"]),
            }
            for row in mappings
        ],
        "questions": question_rows,
    }


def build_generated_file_source_manifest_sqlite(
    con: sqlite3.Connection,
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    file_type = str(file_type or "")
    if file_type == "verbatim":
        return _verbatim_manifest(con, project_id, interview_id)
    if file_type == "formatted_sheet":
        return _formatted_manifest(con, project_id)
    if file_type == "analysis":
        return _analysis_manifest(con, project_id)
    raise GeneratedFileSourceProvenanceSQLiteError(
        f"unsupported generated file_type for source provenance: {file_type}"
    )


def capture_generated_file_source_provenance_sqlite(
    con: sqlite3.Connection,
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    manifest = build_generated_file_source_manifest_sqlite(
        con,
        file_type,
        project_id,
        interview_id=interview_id,
    )
    return {"version": PROVENANCE_VERSION, "sha256": _fingerprint(manifest)}


def validate_generated_file_source_provenance(
    con: sqlite3.Connection,
    expected: dict,
    *,
    file_type: str,
    project_id: int,
    interview_id: int | None = None,
) -> tuple[bool, str]:
    if not isinstance(expected, dict):
        return False, "generated-file source provenance is missing"
    if expected.get("version") != PROVENANCE_VERSION:
        return False, "generated-file source provenance version is missing or unsupported"
    expected_hash = str(expected.get("sha256") or "").strip().lower()
    if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
        return False, "generated-file source provenance hash is missing or invalid"
    try:
        current = capture_generated_file_source_provenance_sqlite(
            con,
            file_type,
            project_id,
            interview_id=interview_id,
        )
    except (GeneratedFileSourceProvenanceSQLiteError, sqlite3.Error) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if current["sha256"] != expected_hash:
        return False, "canonical generated-file inputs changed after generation"
    return True, ""
