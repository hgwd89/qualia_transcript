"""Currentness contract for generated verbatim deliverables.

A verbatim DOCX is immutable history, but the normal delivery path should expose
it only while the rendered source state still matches the current canonical
Interview data.  The source hash deliberately covers output-visible values only;
question mappings and other analysis metadata do not make a verbatim stale.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from models import db
from models.generated_file import GeneratedFile
from models.interview import Interview
from models.speaker_assignment import SpeakerAssignment


VERBATIM_SOURCE_STATE_VERSION = 1
ROLE_LABELS = {
    "moderator": "モデレーター",
    "interviewer": "モデレーター",
    "respondent": "参加者",
    "observer": "オブザーバー",
    "unknown": "話者未確定",
}


@dataclass(frozen=True)
class VerbatimCurrentnessStatus:
    current: bool
    reason: str = ""


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def format_verbatim_time(sec: float | None) -> str:
    if sec is None:
        return "--:--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _display_name(participant) -> str | None:
    if participant is None:
        return None
    return participant.display_name or participant.participant_code or "参加者"


def verbatim_speaker_name(
    interview: Interview,
    segment,
    assignment_map: dict[str, SpeakerAssignment],
) -> str:
    """Render the exact speaker label used by the verbatim report."""
    assignment = assignment_map.get(segment.speaker_label or "")
    role = (
        assignment.speaker_role
        if assignment and assignment.speaker_role
        else (segment.speaker_role or "unknown")
    )

    participant = None
    if assignment and assignment.participant:
        participant = assignment.participant
    elif segment.participant:
        participant = segment.participant
    elif role == "respondent":
        participant = interview.participant

    display = _display_name(participant)
    if display is None:
        if role in {"moderator", "interviewer"} and interview.interviewer_name:
            display = interview.interviewer_name
        else:
            display = ROLE_LABELS.get(role, role or "話者")

    label = segment.speaker_label or ""
    return f"{display} [{label}]" if label and label not in display else display


def _has_quote_flag(segment) -> bool:
    return any(flag.flag_type == "quote" for flag in (segment.segment_flags or []))


def _sorted_segments(interview: Interview):
    return sorted(
        interview.segments,
        key=lambda segment: (
            segment.seq if segment.seq is not None else 10**9,
            segment.start_sec if segment.start_sec is not None else 10**12,
            segment.id or 0,
        ),
    )


def verbatim_source_state(interview: Interview) -> dict[str, Any]:
    """Return only values that affect the rendered verbatim DOCX/download name."""
    project = interview.project
    participant = interview.participant
    assignments = SpeakerAssignment.query.filter_by(interview_id=interview.id).all()
    assignment_map = {
        assignment.speaker_label: assignment
        for assignment in assignments
        if assignment.speaker_label
    }

    title_participant = (
        str(participant.display_name)
        if participant is not None
        else "参加者未設定"
    )
    interview_date = (
        interview.interview_date.isoformat()
        if interview.interview_date is not None
        else "日付未設定"
    )
    participant_code = (
        str(participant.participant_code or "")
        if participant is not None
        else ""
    )

    rows = []
    for segment in _sorted_segments(interview):
        rows.append({
            "start": format_verbatim_time(segment.start_sec),
            "end": format_verbatim_time(segment.end_sec),
            "speaker": verbatim_speaker_name(interview, segment, assignment_map),
            "quote": _has_quote_flag(segment),
            "text": segment.text,
        })

    return {
        "version": VERBATIM_SOURCE_STATE_VERSION,
        "project_name": str(project.name or "") if project is not None else "",
        "project_client": str(project.client or "") if project is not None else "",
        "title_participant": title_participant,
        "participant_code": participant_code,
        "interview_date": interview_date,
        "interviewer_name": str(interview.interviewer_name or ""),
        "segments": rows,
    }


def verbatim_source_state_sha256(interview: Interview) -> str:
    return hashlib.sha256(
        _canonical_json(verbatim_source_state(interview)).encode("utf-8")
    ).hexdigest()


def _generation_params(raw: str | None) -> dict[str, Any]:
    try:
        params = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("verbatim artifact generation metadata is invalid") from exc
    if not isinstance(params, dict):
        raise ValueError("verbatim artifact generation metadata is invalid")
    return params


def _valid_sha256(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"verbatim artifact {label} SHA-256 metadata is missing or invalid")
    return text


def verbatim_artifact_expected_sha256(generated_file: GeneratedFile) -> str:
    params = _generation_params(generated_file.generation_params_json)
    return _valid_sha256(params.get("artifact_sha256"), "byte")


def verbatim_artifact_currentness(generated_file: GeneratedFile) -> VerbatimCurrentnessStatus:
    if generated_file.file_type != "verbatim":
        return VerbatimCurrentnessStatus(True, "")
    if generated_file.interview_id is None:
        return VerbatimCurrentnessStatus(False, "verbatim artifact interview identity is missing")

    try:
        params = _generation_params(generated_file.generation_params_json)
        verbatim_artifact_expected_sha256(generated_file)
        if int(params.get("verbatim_source_state_version")) != VERBATIM_SOURCE_STATE_VERSION:
            return VerbatimCurrentnessStatus(False, "verbatim artifact source-state version is missing or unsupported")
        expected_source_hash = _valid_sha256(
            params.get("verbatim_source_state_sha256"),
            "source-state",
        )
    except (TypeError, ValueError) as exc:
        return VerbatimCurrentnessStatus(False, str(exc))

    interview = db.session.get(Interview, int(generated_file.interview_id))
    if interview is None:
        return VerbatimCurrentnessStatus(False, "verbatim artifact interview no longer exists")
    if int(interview.project_id) != int(generated_file.project_id):
        return VerbatimCurrentnessStatus(False, "verbatim artifact interview belongs to another project")

    current_hash = verbatim_source_state_sha256(interview)
    if current_hash != expected_source_hash:
        return VerbatimCurrentnessStatus(False, "verbatim artifact no longer matches current rendered source state")
    return VerbatimCurrentnessStatus(True, "")


def _sqlite_participant(con: sqlite3.Connection, participant_id: Any) -> dict[str, Any] | None:
    if participant_id is None:
        return None
    row = con.execute(
        "SELECT id, participant_code, display_name FROM participants WHERE id=?",
        (int(participant_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "participant_code": row["participant_code"],
        "display_name": row["display_name"],
    }


def _sqlite_display_name(participant: dict[str, Any] | None) -> str | None:
    if participant is None:
        return None
    return (
        participant.get("display_name")
        or participant.get("participant_code")
        or "参加者"
    )


def _sqlite_speaker_name(
    interview_row: sqlite3.Row,
    interview_participant: dict[str, Any] | None,
    segment_row: sqlite3.Row,
    segment_participant: dict[str, Any] | None,
    assignment: dict[str, Any] | None,
) -> str:
    role = (
        assignment.get("speaker_role")
        if assignment and assignment.get("speaker_role")
        else (segment_row["speaker_role"] or "unknown")
    )
    participant = None
    if assignment and assignment.get("participant") is not None:
        participant = assignment["participant"]
    elif segment_participant is not None:
        participant = segment_participant
    elif role == "respondent":
        participant = interview_participant

    display = _sqlite_display_name(participant)
    if display is None:
        if role in {"moderator", "interviewer"} and interview_row["interviewer_name"]:
            display = interview_row["interviewer_name"]
        else:
            display = ROLE_LABELS.get(str(role), str(role or "話者"))

    label = segment_row["speaker_label"] or ""
    return f"{display} [{label}]" if label and label not in display else str(display)


def verbatim_source_state_sqlite(
    con: sqlite3.Connection,
    interview_id: int,
) -> dict[str, Any]:
    interview = con.execute(
        """
        SELECT id, project_id, participant_id, interview_date, interviewer_name
        FROM interviews WHERE id=?
        """,
        (int(interview_id),),
    ).fetchone()
    if interview is None:
        raise ValueError("verbatim artifact interview no longer exists")
    project = con.execute(
        "SELECT id, name, client FROM projects WHERE id=?",
        (int(interview["project_id"]),),
    ).fetchone()
    if project is None:
        raise ValueError("verbatim artifact project no longer exists")

    interview_participant = _sqlite_participant(con, interview["participant_id"])
    assignments: dict[str, dict[str, Any]] = {}
    for row in con.execute(
        """
        SELECT speaker_label, speaker_role, participant_id
        FROM speaker_assignments
        WHERE interview_id=?
        ORDER BY speaker_label, id
        """,
        (int(interview_id),),
    ).fetchall():
        label = str(row["speaker_label"] or "")
        if not label:
            continue
        assignments[label] = {
            "speaker_role": row["speaker_role"],
            "participant": _sqlite_participant(con, row["participant_id"]),
        }

    segment_rows = con.execute(
        """
        SELECT id, participant_id, speaker_label, speaker_role, start_sec, end_sec, text, seq
        FROM segments
        WHERE interview_id=?
        ORDER BY seq, start_sec, id
        """,
        (int(interview_id),),
    ).fetchall()
    rows = []
    for segment in segment_rows:
        quote_flag = con.execute(
            "SELECT 1 FROM segment_flags WHERE segment_id=? AND flag_type='quote' LIMIT 1",
            (int(segment["id"]),),
        ).fetchone() is not None
        segment_participant = _sqlite_participant(con, segment["participant_id"])
        assignment = assignments.get(str(segment["speaker_label"] or ""))
        rows.append({
            "start": format_verbatim_time(segment["start_sec"]),
            "end": format_verbatim_time(segment["end_sec"]),
            "speaker": _sqlite_speaker_name(
                interview,
                interview_participant,
                segment,
                segment_participant,
                assignment,
            ),
            "quote": quote_flag,
            "text": segment["text"],
        })

    title_participant = (
        str(interview_participant.get("display_name"))
        if interview_participant is not None
        else "参加者未設定"
    )
    participant_code = (
        str(interview_participant.get("participant_code") or "")
        if interview_participant is not None
        else ""
    )
    interview_date = (
        str(interview["interview_date"])
        if interview["interview_date"] is not None
        else "日付未設定"
    )

    return {
        "version": VERBATIM_SOURCE_STATE_VERSION,
        "project_name": str(project["name"] or ""),
        "project_client": str(project["client"] or ""),
        "title_participant": title_participant,
        "participant_code": participant_code,
        "interview_date": interview_date,
        "interviewer_name": str(interview["interviewer_name"] or ""),
        "segments": rows,
    }


def verbatim_source_state_sha256_sqlite(
    con: sqlite3.Connection,
    interview_id: int,
) -> str:
    return hashlib.sha256(
        _canonical_json(verbatim_source_state_sqlite(con, interview_id)).encode("utf-8")
    ).hexdigest()


def validate_verbatim_artifact_currentness_sqlite(
    con: sqlite3.Connection,
    *,
    project_id: int | None,
    interview_id: int | None,
    generation_params_json: str | None,
) -> tuple[bool, str]:
    try:
        if project_id is None or interview_id is None:
            return False, "verbatim artifact project/interview identity is missing"
        params = _generation_params(generation_params_json)
        _valid_sha256(params.get("artifact_sha256"), "byte")
        if int(params.get("verbatim_source_state_version")) != VERBATIM_SOURCE_STATE_VERSION:
            return False, "verbatim artifact source-state version is missing or unsupported"
        expected = _valid_sha256(
            params.get("verbatim_source_state_sha256"),
            "source-state",
        )

        row = con.execute(
            "SELECT project_id FROM interviews WHERE id=?",
            (int(interview_id),),
        ).fetchone()
        if row is None:
            return False, "verbatim artifact interview no longer exists"
        if int(row["project_id"]) != int(project_id):
            return False, "verbatim artifact interview belongs to another project"

        current = verbatim_source_state_sha256_sqlite(con, int(interview_id))
        if current != expected:
            return False, "verbatim artifact no longer matches current rendered source state"
        return True, ""
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        return False, f"verbatim artifact currentness is unprovable: {type(exc).__name__}: {exc}"
