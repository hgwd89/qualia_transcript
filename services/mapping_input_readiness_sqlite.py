from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any


MAPPING_PROVENANCE_VERSION = "mapping-input-v1"
HUMAN_MAPPING_SOURCES = {"human", "manual"}


@dataclass(frozen=True)
class MappingInputReadinessReport:
    blockers: list[dict]
    warnings: list[dict]
    checked_count: int
    current_count: int
    invalid_count: int


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping_manifest(
    con: sqlite3.Connection,
    interview_row: sqlite3.Row,
) -> dict[str, Any]:
    interview_id = int(interview_row["id"])
    project_id = int(interview_row["project_id"])
    flow_id = interview_row["flow_id"]
    if flow_id is None:
        raise ValueError("interview flow is not set")
    flow_id = int(flow_id)

    segments = con.execute(
        """
        SELECT id, seq, speaker_role, text
        FROM main.segments
        WHERE interview_id=? AND speaker_role='respondent'
        ORDER BY seq ASC, id ASC
        """,
        (interview_id,),
    ).fetchall()
    questions = con.execute(
        """
        SELECT
            sec.id AS section_id,
            sec.seq AS section_seq,
            q.id,
            q.seq,
            q.question_code,
            q.question_text
        FROM main.interview_flow_questions q
        JOIN main.interview_flow_sections sec ON sec.id=q.section_id
        WHERE sec.flow_id=?
        ORDER BY sec.seq ASC, sec.id ASC, q.seq ASC, q.id ASC
        """,
        (flow_id,),
    ).fetchall()

    return {
        "version": MAPPING_PROVENANCE_VERSION,
        "project_id": project_id,
        "interview_id": interview_id,
        "flow_id": flow_id,
        "segments": [
            {
                "id": int(row["id"]),
                "seq": int(row["seq"]),
                "speaker_role": str(row["speaker_role"] or ""),
                "text": str(row["text"] or ""),
            }
            for row in segments
        ],
        "questions": [
            {
                "section_id": int(row["section_id"]),
                "section_seq": int(row["section_seq"]),
                "id": int(row["id"]),
                "seq": int(row["seq"]),
                "question_code": row["question_code"],
                "question_text": str(row["question_text"] or ""),
            }
            for row in questions
        ],
    }


def _proof_status(
    raw: str | None,
    manifest: dict[str, Any],
) -> tuple[bool, str]:
    if not raw:
        return False, "AI mapping source provenance is missing"
    try:
        proof = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, "AI mapping source provenance is malformed"
    if not isinstance(proof, dict):
        return False, "AI mapping source provenance is malformed"
    if proof.get("version") != MAPPING_PROVENANCE_VERSION:
        return False, "AI mapping source provenance version is unsupported"

    try:
        project_id = int(proof["project_id"])
        interview_id = int(proof["interview_id"])
        flow_id = int(proof["flow_id"])
        digest = str(proof["sha256"])
    except (KeyError, TypeError, ValueError):
        return False, "AI mapping source provenance is incomplete"

    if project_id != int(manifest["project_id"]):
        return False, "AI mapping source provenance project scope does not match"
    if interview_id != int(manifest["interview_id"]):
        return False, "AI mapping source provenance interview scope does not match"
    if flow_id != int(manifest["flow_id"]):
        return False, "AI mapping source provenance flow scope does not match"
    if digest != _sha256(manifest):
        return False, "mapping canonical source changed after generation"
    return True, ""


def _mapping_rows(
    con: sqlite3.Connection,
    interview_id: int,
    *,
    provenance_table_exists: bool,
) -> list[sqlite3.Row]:
    provenance_select = (
        "p.source_provenance_json AS source_provenance_json"
        if provenance_table_exists
        else "NULL AS source_provenance_json"
    )
    provenance_join = (
        "LEFT JOIN main.utterance_mapping_provenance p ON p.mapping_id=um.id"
        if provenance_table_exists
        else ""
    )
    return con.execute(
        f"""
        SELECT
            um.id,
            um.segment_id,
            um.question_id,
            um.mapped_by,
            {provenance_select}
        FROM main.utterance_mappings um
        JOIN main.segments s ON s.id=um.segment_id
        {provenance_join}
        WHERE s.interview_id=? AND s.speaker_role='respondent'
        ORDER BY um.id ASC
        """,
        (int(interview_id),),
    ).fetchall()


def _interview_status(
    con: sqlite3.Connection,
    interview_row: sqlite3.Row,
    *,
    provenance_table_exists: bool,
) -> tuple[bool, str, dict[str, int]]:
    interview_id = int(interview_row["id"])
    try:
        manifest = _mapping_manifest(con, interview_row)
    except ValueError as exc:
        row_count = int(con.execute(
            """
            SELECT COUNT(*)
            FROM main.utterance_mappings um
            JOIN main.segments s ON s.id=um.segment_id
            WHERE s.interview_id=? AND s.speaker_role='respondent'
            """,
            (interview_id,),
        ).fetchone()[0])
        if row_count:
            return False, str(exc), {
                "respondent_segment_count": 0,
                "mapping_count": row_count,
                "ai_mapping_count": 0,
                "human_mapping_count": 0,
            }
        return True, "", {
            "respondent_segment_count": 0,
            "mapping_count": 0,
            "ai_mapping_count": 0,
            "human_mapping_count": 0,
        }

    expected_segment_ids = {int(row["id"]) for row in manifest["segments"]}
    allowed_question_ids = {int(row["id"]) for row in manifest["questions"]}
    rows = _mapping_rows(
        con,
        interview_id,
        provenance_table_exists=provenance_table_exists,
    )
    counts = {
        "respondent_segment_count": len(expected_segment_ids),
        "mapping_count": len(rows),
        "ai_mapping_count": 0,
        "human_mapping_count": 0,
    }

    requires_mapping = bool(rows) or str(interview_row["status"] or "") == "mapped"
    if not requires_mapping:
        return True, "", counts

    if not expected_segment_ids:
        if rows:
            return False, "mapping rows exist without current respondent segments", counts
        return True, "", counts

    by_segment: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_segment.setdefault(int(row["segment_id"]), []).append(row)

    missing = expected_segment_ids - set(by_segment)
    if missing:
        rendered = ", ".join(str(value) for value in sorted(missing))
        return False, f"mapping input does not cover respondent segment_id(s): {rendered}", counts

    ai_proofs: set[str | None] = set()
    for segment_id in sorted(expected_segment_ids):
        items = by_segment[segment_id]
        effective_question_ids = {
            int(row["question_id"]) if row["question_id"] is not None else None
            for row in items
        }
        if len(effective_question_ids) != 1:
            rendered = ", ".join(
                "null" if value is None else str(value)
                for value in sorted(
                    effective_question_ids,
                    key=lambda value: (-1 if value is None else int(value)),
                )
            )
            return False, (
                f"mapping input has conflicting duplicate rows for respondent segment_id={segment_id}: {rendered}"
            ), counts

        question_id = next(iter(effective_question_ids))
        if question_id is not None and question_id not in allowed_question_ids:
            return False, f"mapping question_id={question_id} is outside the interview flow", counts

        for row in items:
            mapped_by = str(row["mapped_by"] or "ai").strip().lower()
            if mapped_by in HUMAN_MAPPING_SOURCES:
                counts["human_mapping_count"] += 1
                continue
            if mapped_by != "ai":
                return False, f"mapping has unsupported mapped_by value: {mapped_by or '<empty>'}", counts

            counts["ai_mapping_count"] += 1
            ai_proofs.add(row["source_provenance_json"])

    if counts["ai_mapping_count"]:
        if None in ai_proofs or "" in ai_proofs:
            return False, "AI mapping source provenance is missing", counts
        if len(ai_proofs) != 1:
            return False, "AI mapping rows contain mixed source generations", counts
        proof = next(iter(ai_proofs))
        current, reason = _proof_status(proof, manifest)
        if not current:
            return False, reason, counts

    return True, "", counts


def inspect_mapping_input_currentness(
    con: sqlite3.Connection,
    *,
    project_id: int | None = None,
) -> MappingInputReadinessReport:
    """Validate mapping inputs from the caller-owned readiness SQLite snapshot."""
    tables = {
        str(row[0])
        for row in con.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        "interviews",
        "segments",
        "utterance_mappings",
        "interview_flow_sections",
        "interview_flow_questions",
    }
    missing_tables = sorted(required - tables)
    if missing_tables:
        return MappingInputReadinessReport(
            blockers=[{
                "code": "mapping_input_currentness_schema_missing",
                "message": "Mapping input currentness cannot be verified because required tables are missing",
                "context": {"tables": missing_tables},
            }],
            warnings=[],
            checked_count=0,
            current_count=0,
            invalid_count=0,
        )

    mapping_columns = {
        str(row["name"])
        for row in con.execute("PRAGMA main.table_info(utterance_mappings)").fetchall()
    }
    required_mapping_columns = {"id", "segment_id", "question_id", "mapped_by"}
    missing_columns = sorted(required_mapping_columns - mapping_columns)
    if missing_columns:
        return MappingInputReadinessReport(
            blockers=[{
                "code": "mapping_input_currentness_schema_missing",
                "message": "Mapping input currentness cannot be verified because mapping columns are missing",
                "context": {"columns": missing_columns},
            }],
            warnings=[],
            checked_count=0,
            current_count=0,
            invalid_count=0,
        )

    where = ""
    params: tuple[object, ...] = ()
    if project_id is not None:
        where = "WHERE project_id=?"
        params = (int(project_id),)
    interviews = con.execute(
        f"""
        SELECT id, project_id, flow_id, status
        FROM main.interviews
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()

    provenance_table_exists = "utterance_mapping_provenance" in tables
    blockers: list[dict] = []
    current_count = 0
    invalid_count = 0
    checked_count = 0
    for interview in interviews:
        current, reason, counts = _interview_status(
            con,
            interview,
            provenance_table_exists=provenance_table_exists,
        )
        if counts["mapping_count"] == 0 and str(interview["status"] or "") != "mapped":
            continue
        checked_count += 1
        if current:
            current_count += 1
            continue
        invalid_count += 1
        blockers.append({
            "code": "mapping_input_currentness_invalid",
            "message": "Mapping-dependent analysis input is stale, incomplete, or unprovable",
            "context": {
                "project_id": int(interview["project_id"]),
                "interview_id": int(interview["id"]),
                "reason": reason,
                **counts,
            },
        })

    return MappingInputReadinessReport(
        blockers=blockers,
        warnings=[],
        checked_count=checked_count,
        current_count=current_count,
        invalid_count=invalid_count,
    )
