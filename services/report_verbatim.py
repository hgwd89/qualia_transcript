"""
発言録 .docx 生成（タイムスタンプ＋話者名＋発言テキスト）
"""
import os
from datetime import datetime
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
import config
from models.interview import Interview
from models.generated_file import GeneratedFile
from models import db


def _fmt_time(sec: float | None) -> str:
    if sec is None:
        return "--:--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def generate_verbatim(interview_id: int) -> GeneratedFile:
    interview   = Interview.query.get(interview_id)
    participant = interview.participant
    project     = interview.project

    doc = Document()

    # タイトル
    title = doc.add_heading(level=1)
    run = title.add_run(
        f"発言録｜{participant.display_name if participant else '参加者未設定'}"
        f"｜{interview.interview_date or '日付未設定'}"
    )
    run.font.size = Pt(14)

    doc.add_paragraph(f"プロジェクト：{project.name}")
    if project.client:
        doc.add_paragraph(f"クライアント：{project.client}")
    doc.add_paragraph()

    # 発言セクション（フロー順）
    flow = interview.flow
    if flow:
        for section in flow.sections:
            doc.add_heading(section.title, level=2)
            for q in section.questions:
                # この質問に対する発言
                mappings = [
                    um for um in q.utterance_mappings
                    if um.segment and um.segment.interview_id == interview_id
                ]
                if mappings:
                    p = doc.add_paragraph()
                    p.add_run(f"[{q.question_code}] {q.question_text}").bold = True
                    for um in mappings:
                        seg = um.segment
                        row = doc.add_paragraph()
                        speaker = (
                            participant.display_name if participant
                            else seg.speaker_label or "話者"
                        )
                        time_str = f"[{_fmt_time(seg.start_sec)}–{_fmt_time(seg.end_sec)}]"
                        run1 = row.add_run(f"{time_str} {speaker}：")
                        run1.bold = True
                        row.add_run(seg.text)

    # 未分類発言
    unclassified = [
        s for s in interview.segments
        if s.speaker_role == "respondent"
        and all(um.is_unclassified for um in s.utterance_mappings)
        and s.utterance_mappings
    ]
    if unclassified:
        doc.add_heading("【未分類発言】", level=2)
        for seg in unclassified:
            row = doc.add_paragraph()
            row.add_run(f"[{_fmt_time(seg.start_sec)}] ").bold = True
            row.add_run(seg.text)

    # 保存
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"発言録_{participant.participant_code if participant else 'unknown'}_{ts}.docx"
    out_dir  = os.path.join(config.OUTPUT_DIR, str(interview.project_id))
    os.makedirs(out_dir, exist_ok=True)
    full_path = os.path.join(out_dir, filename)
    doc.save(full_path)

    rel_path = os.path.join(str(interview.project_id), filename)
    gf = GeneratedFile(
        project_id=project.id,
        interview_id=interview_id,
        file_type="verbatim",
        file_format="docx",
        original_filename=filename,
        stored_path=rel_path,
    )
    db.session.add(gf)
    db.session.commit()
    return gf
