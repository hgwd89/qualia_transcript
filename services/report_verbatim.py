"""
発言録 .docx 生成。

納品用の発言録は分析用マッピングに依存させず、Interview に属する全 Segment を
seq 順に欠落なく出力する。質問別の整理は formatted sheet 側の責務とする。
"""
import json
import os
from datetime import datetime

from docx import Document
from docx.shared import Pt
from sqlalchemy import text

import config
from models import db
from models.generated_file import GeneratedFile
from models.interview import Interview
from models.speaker_assignment import SpeakerAssignment
from services.file_manager import (
    open_output_target_for_write,
    prepare_output_target,
    register_generated_file,
)
from services.formal_artifact_integrity import sha256_managed_generation
from services.verbatim_artifact_currentness import (
    VERBATIM_SOURCE_STATE_VERSION,
    format_verbatim_time,
    verbatim_source_state_sha256,
    verbatim_speaker_name,
)


def _fmt_time(sec: float | None) -> str:
    return format_verbatim_time(sec)


def _has_quote_flag(seg) -> bool:
    return any(f.flag_type == "quote" for f in (seg.segment_flags or []))


def _speaker_name(interview: Interview, seg, assignment_map: dict) -> str:
    return verbatim_speaker_name(interview, seg, assignment_map)


def _begin_verbatim_export_snapshot() -> None:
    """Serialize rendered-source validation through GeneratedFile registration."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("verbatim export requires a clean database session")
    db.session.rollback()
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))


def generate_verbatim(interview_id: int) -> GeneratedFile:
    _begin_verbatim_export_snapshot()
    try:
        interview = db.session.get(Interview, int(interview_id))
        if not interview:
            raise ValueError("interview が見つかりません")

        source_state_sha256 = verbatim_source_state_sha256(interview)
        participant = interview.participant
        project = interview.project
        segments = sorted(
            interview.segments,
            key=lambda s: (
                s.seq if s.seq is not None else 10**9,
                s.start_sec if s.start_sec is not None else 10**12,
                s.id or 0,
            ),
        )

        assignments = SpeakerAssignment.query.filter_by(interview_id=interview.id).all()
        assignment_map = {a.speaker_label: a for a in assignments if a.speaker_label}

        doc = Document()

        title = doc.add_heading(level=1)
        run = title.add_run(
            f"発言録｜{participant.display_name if participant else '参加者未設定'}"
            f"｜{interview.interview_date or '日付未設定'}"
        )
        run.font.size = Pt(14)

        doc.add_paragraph(f"プロジェクト：{project.name}")
        if project.client:
            doc.add_paragraph(f"クライアント：{project.client}")
        if participant and participant.participant_code:
            doc.add_paragraph(f"参加者ID：{participant.participant_code}")
        if interview.interviewer_name:
            doc.add_paragraph(f"モデレーター：{interview.interviewer_name}")
        doc.add_paragraph(f"収録Segment数：{len(segments)}")
        doc.add_paragraph()

        doc.add_heading("逐語発言録（時系列）", level=2)

        for seg in segments:
            row = doc.add_paragraph()
            time_str = f"[{_fmt_time(seg.start_sec)}–{_fmt_time(seg.end_sec)}]"
            speaker = _speaker_name(interview, seg, assignment_map)
            lead = row.add_run(f"{time_str} {speaker}：")
            lead.bold = True
            if _has_quote_flag(seg):
                quote_mark = row.add_run("★引用候補 ")
                quote_mark.bold = True
            row.add_run(seg.text)

        if not segments:
            doc.add_paragraph("（発言Segmentがありません）")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"発言録_{participant.participant_code if participant else 'unknown'}_{ts}.docx"
        target = prepare_output_target(interview.project_id, filename)
        opened = open_output_target_for_write(target)
        try:
            doc.save(opened.stream)
        finally:
            opened.close()

        artifact_sha256 = sha256_managed_generation(
            config.OUTPUT_DIR,
            target.stored_path,
            target.written_stat,
        )
        params = {
            "verbatim_source_state_version": VERBATIM_SOURCE_STATE_VERSION,
            "verbatim_source_state_sha256": source_state_sha256,
            "artifact_sha256": artifact_sha256,
        }
        return register_generated_file(
            target,
            project_id=project.id,
            interview_id=interview_id,
            file_type="verbatim",
            file_format="docx",
            generation_params_json=json.dumps(params, ensure_ascii=False),
        )
    except Exception:
        db.session.rollback()
        raise
