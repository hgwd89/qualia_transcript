"""
整形シート .xlsx 生成
行：質問項目、列：参加者、セル：該当発言テキスト
"""
import os
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import config
from models.project import Project
from models.participant import Participant
from models.interview_flow import InterviewFlowQuestion
from models.interview import Interview
from models.segment import Segment, UtteranceMapping
from models.generated_file import GeneratedFile
from models import db


_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_KEY_FILL    = PatternFill("solid", fgColor="FCE4D6")
_THIN        = Side(style="thin", color="AAAAAA")
_BORDER      = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def generate_formatted_sheet(project_id: int) -> GeneratedFile:
    project      = Project.query.get(project_id)
    participants = Participant.query.filter_by(project_id=project_id).all()
    interviews   = Interview.query.filter_by(project_id=project_id).all()

    # 参加者→インタビュー の対応マップ
    p_to_interview = {iv.participant_id: iv for iv in interviews if iv.participant_id}

    # フロー（最初のフローを使用）
    flows = project.interview_flows
    if not flows:
        raise ValueError("インタビューフローが設定されていません")
    flow = flows[0]

    wb = Workbook()

    # ─── シート1：整形シート ───
    ws = wb.active
    ws.title = "整形シート"

    # ヘッダー行
    ws.cell(1, 1, "セクション").font       = Font(bold=True, color="FFFFFF")
    ws.cell(1, 1).fill                     = _HEADER_FILL
    ws.cell(1, 2, "質問コード").font       = Font(bold=True, color="FFFFFF")
    ws.cell(1, 2).fill                     = _HEADER_FILL
    ws.cell(1, 3, "質問テキスト").font     = Font(bold=True, color="FFFFFF")
    ws.cell(1, 3).fill                     = _HEADER_FILL

    col_offset = 4
    for i, p in enumerate(participants):
        c = ws.cell(1, col_offset + i,
                    f"{p.participant_code}\n{p.display_name or ''}")
        c.font      = Font(bold=True, color="FFFFFF")
        c.fill      = _HEADER_FILL
        c.alignment = Alignment(wrap_text=True, horizontal="center")

    # 質問行
    row = 2
    for section in flow.sections:
        for q in section.questions:
            ws.cell(row, 1, section.title)
            ws.cell(row, 2, q.question_code)
            ws.cell(row, 3, q.question_text).alignment = Alignment(wrap_text=True)

            if q.is_key_question:
                for col in range(1, col_offset + len(participants)):
                    ws.cell(row, col).fill = _KEY_FILL

            for i, p in enumerate(participants):
                iv = p_to_interview.get(p.id)
                if not iv:
                    continue
                # この質問 × このインタビューの発言
                mappings = (
                    UtteranceMapping.query
                    .filter_by(question_id=q.id)
                    .join(Segment, UtteranceMapping.segment_id == Segment.id)
                    .filter(Segment.interview_id == iv.id,
                            Segment.speaker_role == "respondent")
                    .all()
                )
                texts = "\n".join(f"・{m.segment.text}" for m in mappings)
                c = ws.cell(row, col_offset + i, texts)
                c.alignment = Alignment(wrap_text=True, vertical="top")

            for col in range(1, col_offset + len(participants)):
                ws.cell(row, col).border = _BORDER

            row += 1

    # 列幅
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 40
    for i in range(len(participants)):
        ws.column_dimensions[get_column_letter(col_offset + i)].width = 35

    ws.freeze_panes = "D2"

    # ─── シート2：未分類発言 ───
    ws2 = wb.create_sheet("未分類発言")
    ws2.append(["参加者", "発言テキスト", "開始時刻"])
    for iv in interviews:
        p = iv.participant
        code = p.participant_code if p else "?"
        for seg in iv.segments:
            if seg.speaker_role != "respondent":
                continue
            if not seg.utterance_mappings:
                continue
            if all(um.is_unclassified for um in seg.utterance_mappings):
                ws2.append([code, seg.text,
                             _fmt_time(seg.start_sec) if seg.start_sec else ""])

    # 保存
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"整形シート_{project.name}_{ts}.xlsx"
    out_dir  = os.path.join(config.OUTPUT_DIR, str(project_id))
    os.makedirs(out_dir, exist_ok=True)
    full_path = os.path.join(out_dir, filename)
    wb.save(full_path)

    rel_path = os.path.join(str(project_id), filename)
    gf = GeneratedFile(
        project_id=project_id,
        file_type="formatted_sheet",
        file_format="xlsx",
        original_filename=filename,
        stored_path=rel_path,
    )
    db.session.add(gf)
    db.session.commit()
    return gf


def _fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
