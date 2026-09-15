"""Read-only SQLite validation for persisted AI mapping source provenance.

This module mirrors the canonical ``mapping-input-v1`` manifest without importing
Flask or SQLAlchemy. Final production-readiness can therefore prove that every AI
mapping still present in a delivery-state interview was generated from the
current canonical respondent segments and flow questions.

Human/manual mapping overrides are intentionally outside this contract. They are
canonical human edits and may coexist with a still-current subset of AI mappings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
from typing import Any


MAPPING_PROVENANCE_VERSION = "mapping-input-v1"
FINAL_INTERVIEW_STATUSES = {"mapped", "analyzed", "done"}


@dataclass
class MappingReadinessResult:
    blockers: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    checked_interview_count: int = 0
    ai_mapping_count: int = 0


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _tables(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def build_mapping_source_manifest(
    con: sqlite3.Connection,
    interview_id: int,
) -> dict[str, Any]:
    interview = con.execute(
        "SELECT id, project_id, flow_id FROM interviews WHERE id=?",
        (int(interview_id),),
    ).fetchone()
    if interview is None:
        raise ValueError("interview not found")
    if interview["flow_id"] is None:
        raise ValueError("interview flow is not set")

    segment_rows = con.execute(
        """
        SELECT id, seq, speaker_role, text
        FROM segments
        WHERE interview_id=? AND speaker_role='respondent'
        ORDER BY seq, id
        """,
        (int(interview_id),),
    ).fetchall()
    question_rows = con.execute(
        """
        SELECT
            sec.id AS section_id,
            sec.seq AS section_seq,
            q.id,
            q.seq,
            q.question_code,
            q.question_text
        FROM interview_flow_questions q
        JOIN interview_flow_sections sec ON sec.id=q.section_id
        WHERE sec.flow_id=?
        ORDER BY sec.seq, sec.id, q.seq, q.id
        """,
        (int(interview["flow_id"]),),
    ).fetchall()

    return {
        "version": MAPPING_PROVENANCE_VERSION,
        "project_id": int(interview["project_id"]),
        "interview_id": int(interview["id"]),
        "flow_id": int(interview["flow_id"]),
        "segments": [
            {
                "id": int(row["id"]),
                "seq": int(row["seq"]),
                "speaker_role": str(row["speaker_role"] or ""),
                "text": str(row["text"] or ""),
            }
            for row in segment_rows
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
            for row in question_rows
        ],
    }


def mapping_provenance_for_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": MAPPING_PROVENANCE_VERSION,
        "project_id": int(manifest["project_id"]),
        "interview_id": int(manifest["interview_id"]),
        "flow_id": int(manifest["flow_id"]),
        "sha256": _fingerprint(manifest),
    }


def _issue(code: str, message: str, **context: Any) -> dict:
    item: dict[str, Any] = {"code": code, "message": message}
    if context:
        item["context"] = context
    return item


def inspect_mapping_provenance_readiness(
    con: sqlite3.Connection,
    *,
    project_id: int | None = None,
) -> MappingReadinessResult:
    """Validate current AI mappings for delivery-state interviews.

    The validator is deliberately fail-closed for AI rows: missing, malformed,
    mixed, wrong-scope, or stale generation proof is a blocker. Human mapping
    rows do not require AI provenance and may legitimately replace a subset of a
    previously generated AI mapping batch.
    """
    result = MappingReadinessResult()
    tables = _tables(con)
    required = {
        "interviews",
        "segments",
        "interview_flow_sections",
        "interview_flow_questions",
        "utterance_mappings",
    }
    missing_core = sorted(required - tables)
    if missing_core:
        result.blockers.append(_issue(
            "mapping_provenance_core_tables_missing",
            "Mapping provenance readiness cannot be evaluated because core tables are missing",
            tables=missing_core,
        ))
        return result

    scope_sql = ""
    params: list[Any] = []
    if project_id is not None:
        scope_sql = " AND i.project_id=?"
        params.append(int(project_id))

    interview_rows = con.execute(
        f"""
        SELECT i.id, i.project_id, i.flow_id, i.status
        FROM interviews i
        WHERE i.status IN ('mapped','analyzed','done')
          {scope_sql}
          AND EXISTS (
              SELECT 1
              FROM utterance_mappings um
              JOIN segments s ON s.id=um.segment_id
              WHERE s.interview_id=i.id AND um.mapped_by='ai'
          )
        ORDER BY i.id
        """,
        tuple(params),
    ).fetchall()

    if "utterance_mapping_provenance" not in tables:
        if interview_rows:
            ai_count = 0
            for interview in interview_rows:
                ai_count += int(con.execute(
                    """
                    SELECT COUNT(*)
                    FROM utterance_mappings um
                    JOIN segments s ON s.id=um.segment_id
                    WHERE s.interview_id=? AND um.mapped_by='ai'
                    """,
                    (int(interview["id"]),),
                ).fetchone()[0])
            result.ai_mapping_count = ai_count
            result.blockers.append(_issue(
                "mapping_provenance_table_missing",
                "AI mappings exist but the mapping provenance sidecar table is missing; start the upgraded app once and regenerate mappings before professional use",
                interview_ids=[int(row["id"]) for row in interview_rows[:100]],
                interview_count=len(interview_rows),
                ai_mapping_count=ai_count,
            ))
        return result

    for interview in interview_rows:
        interview_id = int(interview["id"])
        result.checked_interview_count += 1
        rows = con.execute(
            """
            SELECT
                um.id AS mapping_id,
                um.segment_id,
                ump.source_provenance_json
            FROM utterance_mappings um
            JOIN segments s ON s.id=um.segment_id
            LEFT JOIN utterance_mapping_provenance ump ON ump.mapping_id=um.id
            WHERE s.interview_id=? AND um.mapped_by='ai'
            ORDER BY um.id
            """,
            (interview_id,),
        ).fetchall()
        result.ai_mapping_count += len(rows)
        if not rows:
            continue

        mapping_ids = [int(row["mapping_id"]) for row in rows]
        proofs = {row["source_provenance_json"] for row in rows}
        if len(proofs) != 1:
            result.blockers.append(_issue(
                "ai_mapping_mixed_source_provenance",
                "Current AI mappings for one interview contain multiple source generations",
                interview_id=interview_id,
                mapping_ids=mapping_ids[:200],
                mapping_count=len(rows),
            ))
            continue

        raw_proof = next(iter(proofs))
        if not raw_proof:
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_missing",
                "Current AI mappings have no source-generation proof",
                interview_id=interview_id,
                mapping_ids=mapping_ids[:200],
                mapping_count=len(rows),
            ))
            continue
        try:
            proof = json.loads(str(raw_proof))
        except (TypeError, ValueError, json.JSONDecodeError):
            proof = None
        if not isinstance(proof, dict):
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_malformed",
                "Current AI mapping source-generation proof is malformed",
                interview_id=interview_id,
                mapping_ids=mapping_ids[:200],
                mapping_count=len(rows),
            ))
            continue

        if str(proof.get("version") or "") != MAPPING_PROVENANCE_VERSION:
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_version_unsupported",
                "Current AI mapping source-generation proof uses an unsupported version",
                interview_id=interview_id,
                version=proof.get("version"),
            ))
            continue

        try:
            stored_project_id = int(proof["project_id"])
            stored_interview_id = int(proof["interview_id"])
            stored_flow_id = int(proof["flow_id"])
            stored_hash = str(proof["sha256"]).strip().lower()
        except (KeyError, TypeError, ValueError):
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_incomplete",
                "Current AI mapping source-generation proof is incomplete",
                interview_id=interview_id,
            ))
            continue

        if len(stored_hash) != 64 or any(ch not in "0123456789abcdef" for ch in stored_hash):
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_hash_invalid",
                "Current AI mapping source-generation hash is invalid",
                interview_id=interview_id,
            ))
            continue

        if (
            stored_project_id != int(interview["project_id"])
            or stored_interview_id != interview_id
            or interview["flow_id"] is None
            or stored_flow_id != int(interview["flow_id"])
        ):
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_scope_mismatch",
                "Current AI mapping source-generation proof no longer matches interview/project/flow scope",
                interview_id=interview_id,
                stored_project_id=stored_project_id,
                current_project_id=int(interview["project_id"]),
                stored_interview_id=stored_interview_id,
                stored_flow_id=stored_flow_id,
                current_flow_id=(int(interview["flow_id"]) if interview["flow_id"] is not None else None),
            ))
            continue

        try:
            manifest = build_mapping_source_manifest(con, interview_id)
        except (TypeError, ValueError) as exc:
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_unrebuildable",
                "Current AI mapping source generation cannot be rebuilt from canonical data",
                interview_id=interview_id,
                error=f"{type(exc).__name__}: {exc}",
            ))
            continue

        if _fingerprint(manifest) != stored_hash:
            result.blockers.append(_issue(
                "ai_mapping_source_provenance_stale",
                "Current AI mappings were generated from an older canonical segment/question generation",
                interview_id=interview_id,
                mapping_ids=mapping_ids[:200],
                mapping_count=len(rows),
            ))
            continue

        expected_segment_ids = {int(row["id"]) for row in manifest["segments"]}
        ai_segment_ids = {int(row["segment_id"]) for row in rows}
        outside = sorted(ai_segment_ids - expected_segment_ids)
        if outside:
            result.blockers.append(_issue(
                "ai_mapping_segment_scope_stale",
                "Current AI mappings reference segments that are no longer respondent source inputs",
                interview_id=interview_id,
                segment_ids=outside[:200],
                count=len(outside),
            ))

    # Project-scoped readiness shadows utterance_mappings with a TEMP VIEW. Use
    # main explicitly here so valid provenance rows from other projects are not
    # falsely reported as orphans. Orphan referential integrity remains global,
    # consistent with the other database-level FK checks in project readiness.
    if "utterance_mapping_provenance" in tables:
        orphan_rows = con.execute(
            """
            SELECT ump.mapping_id
            FROM main.utterance_mapping_provenance ump
            LEFT JOIN main.utterance_mappings um ON um.id=ump.mapping_id
            WHERE um.id IS NULL
            ORDER BY ump.mapping_id
            """
        ).fetchall()
        if orphan_rows:
            result.blockers.append(_issue(
                "mapping_provenance_orphans",
                "Mapping provenance rows reference missing mapping rows",
                mapping_ids=[int(row["mapping_id"]) for row in orphan_rows[:200]],
                count=len(orphan_rows),
            ))

    return result
