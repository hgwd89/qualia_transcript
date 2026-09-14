"""Read-only SQLite mirror of formal AI-analysis source provenance.

This module intentionally depends only on sqlite3-compatible connections and
plain dictionaries. Production-readiness audits can therefore validate the same
`analysis-input-v1` contract without initializing Flask/SQLAlchemy or writing to
the application database.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


PROVENANCE_KEY = "source_provenance"
PROVENANCE_VERSION = "analysis-input-v1"
SUPPORTED_ANALYSIS_TYPES = {
    "per_question",
    "per_participant",
    "cross_participant",
    "integrated",
}
ANALYSIS_READY_INTERVIEW_STATUSES = {"mapped", "analyzed", "done"}


class SQLiteAnalysisSourceProvenanceError(ValueError):
    pass


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _segment_manifest(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "seq": int(row["seq"]),
        "text": str(row["text"] or ""),
    }


def _question_manifest(con: sqlite3.Connection, question_id: int) -> dict[str, Any]:
    row = con.execute(
        """
        SELECT q.id, q.section_id, q.question_code, q.question_text, q.seq,
               sec.flow_id, f.project_id
        FROM interview_flow_questions q
        JOIN interview_flow_sections sec ON sec.id=q.section_id
        JOIN interview_flows f ON f.id=sec.flow_id
        WHERE q.id=?
        """,
        (int(question_id),),
    ).fetchone()
    if row is None:
        raise SQLiteAnalysisSourceProvenanceError("question not found")
    return {
        "id": int(row["id"]),
        "flow_id": int(row["flow_id"]),
        "section_id": int(row["section_id"]),
        "question_code": str(row["question_code"] or ""),
        "question_text": str(row["question_text"] or ""),
        "seq": int(row["seq"]),
        "project_id": int(row["project_id"]),
    }


def _mapped_respondent_segments(
    con: sqlite3.Connection,
    interview_id: int,
    question_id: int,
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT s.id, s.seq, s.text
        FROM segments s
        JOIN utterance_mappings um ON um.segment_id=s.id
        WHERE s.interview_id=?
          AND s.speaker_role='respondent'
          AND um.question_id=?
        ORDER BY s.seq, s.id
        """,
        (int(interview_id), int(question_id)),
    ).fetchall()
    by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        sid = int(row["id"])
        if sid not in by_id:
            by_id[sid] = _segment_manifest(row)
    return list(by_id.values())


def _respondent_segments(con: sqlite3.Connection, interview_id: int) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT id, seq, text
        FROM segments
        WHERE interview_id=? AND speaker_role='respondent'
        ORDER BY seq, id
        """,
        (int(interview_id),),
    ).fetchall()
    return [_segment_manifest(row) for row in rows]


def _interview_with_participant(con: sqlite3.Connection, interview_id: int) -> sqlite3.Row:
    row = con.execute(
        """
        SELECT i.id, i.project_id, i.flow_id, i.participant_id, i.status,
               p.participant_code, p.display_name
        FROM interviews i
        LEFT JOIN participants p ON p.id=i.participant_id
        WHERE i.id=?
        """,
        (int(interview_id),),
    ).fetchone()
    if row is None:
        raise SQLiteAnalysisSourceProvenanceError("interview not found")
    return row


def _per_question_manifest(
    con: sqlite3.Connection,
    project_id: int,
    interview_id: int | None,
    question_id: int | None,
) -> dict[str, Any]:
    if interview_id is None or question_id is None:
        raise SQLiteAnalysisSourceProvenanceError("per_question scope is incomplete")
    interview = _interview_with_participant(con, interview_id)
    question = _question_manifest(con, question_id)
    if int(interview["project_id"]) != int(project_id):
        raise SQLiteAnalysisSourceProvenanceError("interview project changed")
    if interview["flow_id"] is None or int(interview["flow_id"]) != int(question["flow_id"]):
        raise SQLiteAnalysisSourceProvenanceError("question no longer belongs to interview flow")
    if int(question["project_id"]) != int(project_id):
        raise SQLiteAnalysisSourceProvenanceError("question project changed")

    question.pop("project_id", None)
    participant_code = str(interview["participant_code"] or "P??")
    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "per_question",
        "project_id": int(project_id),
        "interview": {
            "id": int(interview["id"]),
            "flow_id": int(interview["flow_id"]),
            "participant_id": (
                int(interview["participant_id"])
                if interview["participant_id"] is not None else None
            ),
            "participant_code": participant_code,
        },
        "question": question,
        "segments": _mapped_respondent_segments(con, interview_id, question_id),
    }


def _per_participant_manifest(
    con: sqlite3.Connection,
    project_id: int,
    interview_id: int | None,
) -> dict[str, Any]:
    if interview_id is None:
        raise SQLiteAnalysisSourceProvenanceError("per_participant scope is incomplete")
    interview = _interview_with_participant(con, interview_id)
    if int(interview["project_id"]) != int(project_id):
        raise SQLiteAnalysisSourceProvenanceError("interview project changed")

    if interview["participant_id"] is not None:
        code = str(interview["participant_code"] or "P??")
        display_name = str(interview["display_name"] or code)
    else:
        code = "P??"
        display_name = "参加者未設定"

    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "per_participant",
        "project_id": int(project_id),
        "interview": {
            "id": int(interview["id"]),
            "flow_id": int(interview["flow_id"]) if interview["flow_id"] is not None else None,
            "participant_id": (
                int(interview["participant_id"])
                if interview["participant_id"] is not None else None
            ),
            "participant_code": code,
            "participant_display_name": display_name,
        },
        "segments": _respondent_segments(con, interview_id),
    }


def _cross_participant_manifest(
    con: sqlite3.Connection,
    project_id: int,
    question_id: int | None,
) -> dict[str, Any]:
    if question_id is None:
        raise SQLiteAnalysisSourceProvenanceError("cross_participant scope is incomplete")
    question = _question_manifest(con, question_id)
    if int(question["project_id"]) != int(project_id):
        raise SQLiteAnalysisSourceProvenanceError("question project changed")
    flow_id = int(question["flow_id"])
    question.pop("project_id", None)

    interviews = con.execute(
        """
        SELECT i.id, i.participant_id, p.id AS resolved_participant_id,
               p.participant_code
        FROM interviews i
        LEFT JOIN participants p ON p.id=i.participant_id
        WHERE i.project_id=? AND i.flow_id=?
        ORDER BY i.id
        """,
        (int(project_id), flow_id),
    ).fetchall()
    participants: list[dict[str, Any]] = []
    for interview in interviews:
        if interview["resolved_participant_id"] is None:
            continue
        segments = _mapped_respondent_segments(con, int(interview["id"]), int(question_id))
        if not segments:
            continue
        participants.append({
            "interview_id": int(interview["id"]),
            "participant_id": int(interview["resolved_participant_id"]),
            "participant_code": str(interview["participant_code"] or ""),
            "segments": segments,
        })
    if not participants:
        raise SQLiteAnalysisSourceProvenanceError("cross-participant source is empty")

    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "cross_participant",
        "project_id": int(project_id),
        "question": question,
        "participants": participants,
    }


def _resolve_integrated_scope(
    con: sqlite3.Connection,
    project_id: int,
) -> tuple[int, list[sqlite3.Row]]:
    flow_rows = con.execute(
        "SELECT id FROM interview_flows WHERE project_id=? ORDER BY id",
        (int(project_id),),
    ).fetchall()
    if len(flow_rows) == 0:
        raise SQLiteAnalysisSourceProvenanceError("integrated project flow is missing")
    if len(flow_rows) != 1:
        raise SQLiteAnalysisSourceProvenanceError("integrated project flow is ambiguous")
    flow_id = int(flow_rows[0]["id"])

    interviews = con.execute(
        """
        SELECT i.id, i.flow_id, i.participant_id, i.status,
               p.id AS resolved_participant_id, p.participant_code
        FROM interviews i
        LEFT JOIN participants p ON p.id=i.participant_id
        WHERE i.project_id=? AND i.participant_id IS NOT NULL
        ORDER BY i.id
        """,
        (int(project_id),),
    ).fetchall()
    if not interviews:
        raise SQLiteAnalysisSourceProvenanceError("integrated participant interviews are missing")

    for interview in interviews:
        if interview["flow_id"] is None:
            raise SQLiteAnalysisSourceProvenanceError("integrated interview flow is missing")
        if int(interview["flow_id"]) != flow_id:
            raise SQLiteAnalysisSourceProvenanceError("integrated interview flow changed")
        if str(interview["status"] or "") not in ANALYSIS_READY_INTERVIEW_STATUSES:
            raise SQLiteAnalysisSourceProvenanceError("integrated interview is no longer analysis-ready")

        has_evidence = con.execute(
            """
            SELECT 1
            FROM utterance_mappings um
            JOIN segments s ON s.id=um.segment_id
            JOIN interview_flow_questions q ON q.id=um.question_id
            JOIN interview_flow_sections sec ON sec.id=q.section_id
            WHERE s.interview_id=?
              AND s.speaker_role='respondent'
              AND COALESCE(um.is_unclassified,0)=0
              AND sec.flow_id=?
            LIMIT 1
            """,
            (int(interview["id"]), flow_id),
        ).fetchone()
        if has_evidence is None:
            raise SQLiteAnalysisSourceProvenanceError(
                "integrated interview no longer has mapped respondent evidence"
            )

    return flow_id, interviews


def _integrated_manifest(con: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    project = con.execute(
        "SELECT id, name, client, research_objective FROM projects WHERE id=?",
        (int(project_id),),
    ).fetchone()
    if project is None:
        raise SQLiteAnalysisSourceProvenanceError("project not found")

    flow_id, interviews = _resolve_integrated_scope(con, project_id)
    source_interviews = [
        {
            "id": int(interview["id"]),
            "participant_id": (
                int(interview["resolved_participant_id"])
                if interview["resolved_participant_id"] is not None else None
            ),
            "participant_code": (
                str(interview["participant_code"] or "")
                if interview["resolved_participant_id"] is not None else ""
            ),
        }
        for interview in interviews
    ]

    section_rows = con.execute(
        """
        SELECT id, title, seq
        FROM interview_flow_sections
        WHERE flow_id=?
        ORDER BY seq, id
        """,
        (flow_id,),
    ).fetchall()
    sections: list[dict[str, Any]] = []
    for section in section_rows:
        question_rows = con.execute(
            """
            SELECT id, question_code, question_text, seq
            FROM interview_flow_questions
            WHERE section_id=?
            ORDER BY seq, id
            """,
            (int(section["id"]),),
        ).fetchall()
        questions: list[dict[str, Any]] = []
        for question in question_rows:
            sources: list[dict[str, Any]] = []
            for interview in interviews:
                if interview["resolved_participant_id"] is None:
                    continue
                segments = _mapped_respondent_segments(
                    con,
                    int(interview["id"]),
                    int(question["id"]),
                )
                if not segments:
                    continue
                sources.append({
                    "interview_id": int(interview["id"]),
                    "participant_id": int(interview["resolved_participant_id"]),
                    "participant_code": str(interview["participant_code"] or ""),
                    "segments": segments,
                })
            questions.append({
                "id": int(question["id"]),
                "question_code": str(question["question_code"] or ""),
                "question_text": str(question["question_text"] or ""),
                "seq": int(question["seq"]),
                "sources": sources,
            })
        sections.append({
            "id": int(section["id"]),
            "title": str(section["title"] or ""),
            "seq": int(section["seq"]),
            "questions": questions,
        })

    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "integrated",
        "project": {
            "id": int(project["id"]),
            "name": str(project["name"] or ""),
            "client": str(project["client"] or ""),
            "research_objective": str(project["research_objective"] or ""),
        },
        "flow_id": flow_id,
        "source_interviews": source_interviews,
        "sections": sections,
    }


def build_analysis_source_manifest(
    con: sqlite3.Connection,
    analysis_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
    question_id: int | None = None,
) -> dict[str, Any]:
    analysis_type = str(analysis_type or "")
    project_id = int(project_id)
    if analysis_type == "per_question":
        return _per_question_manifest(con, project_id, interview_id, question_id)
    if analysis_type == "per_participant":
        return _per_participant_manifest(con, project_id, interview_id)
    if analysis_type == "cross_participant":
        return _cross_participant_manifest(con, project_id, question_id)
    if analysis_type == "integrated":
        return _integrated_manifest(con, project_id)
    raise SQLiteAnalysisSourceProvenanceError(
        f"unsupported formal analysis type: {analysis_type or '(empty)'}"
    )


def validate_analysis_source_provenance(
    con: sqlite3.Connection,
    *,
    analysis_type: str,
    project_id: int | None,
    interview_id: int | None,
    question_id: int | None,
    content: dict[str, Any],
) -> tuple[bool, str]:
    if project_id is None:
        return False, "analysis project_id is missing"
    if str(analysis_type or "") not in SUPPORTED_ANALYSIS_TYPES:
        return False, f"unsupported formal analysis type: {analysis_type or '(empty)'}"

    stored = content.get(PROVENANCE_KEY) if isinstance(content, dict) else None
    if not isinstance(stored, dict):
        return False, "analysis has no generation source provenance"
    if str(stored.get("version") or "") != PROVENANCE_VERSION:
        return False, "analysis source provenance version is unsupported"
    expected_hash = str(stored.get("sha256") or "").strip().lower()
    if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
        return False, "analysis source provenance hash is invalid"

    try:
        manifest = build_analysis_source_manifest(
            con,
            str(analysis_type),
            int(project_id),
            interview_id=(int(interview_id) if interview_id is not None else None),
            question_id=(int(question_id) if question_id is not None else None),
        )
    except (SQLiteAnalysisSourceProvenanceError, TypeError, ValueError) as exc:
        return False, f"analysis source provenance cannot be rebuilt: {exc}"

    current_hash = _fingerprint(manifest)
    if current_hash != expected_hash:
        return False, "analysis source provenance no longer matches canonical inputs"
    return True, ""
