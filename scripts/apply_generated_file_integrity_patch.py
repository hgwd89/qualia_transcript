from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if new in text:
        raise SystemExit(f"patch already applied: {path}")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# verbatim
replace_once(
    "services/report_verbatim.py",
    "from models.speaker_assignment import SpeakerAssignment\n",
    "from models.speaker_assignment import SpeakerAssignment\n"
    "from services.file_manager import prepare_output_target, register_generated_file\n",
)
replace_once(
    "services/report_verbatim.py",
    '''    filename = f"発言録_{participant.participant_code if participant else 'unknown'}_{ts}.docx"\n    out_dir = os.path.join(config.OUTPUT_DIR, str(interview.project_id))\n    os.makedirs(out_dir, exist_ok=True)\n    full_path = os.path.join(out_dir, filename)\n    doc.save(full_path)\n\n    rel_path = os.path.join(str(interview.project_id), filename)\n    gf = GeneratedFile(\n        project_id=project.id,\n        interview_id=interview_id,\n        file_type="verbatim",\n        file_format="docx",\n        original_filename=filename,\n        stored_path=rel_path,\n    )\n    db.session.add(gf)\n    db.session.commit()\n    return gf\n''',
    '''    filename = f"発言録_{participant.participant_code if participant else 'unknown'}_{ts}.docx"\n    target = prepare_output_target(interview.project_id, filename)\n    doc.save(target.full_path)\n    return register_generated_file(\n        target,\n        project_id=project.id,\n        interview_id=interview_id,\n        file_type="verbatim",\n        file_format="docx",\n    )\n''',
)

# formatted sheet
replace_once(
    "services/report_formatted.py",
    "from models.segment import Segment, UtteranceMapping\n",
    "from models.segment import Segment, UtteranceMapping\n"
    "from services.file_manager import prepare_output_target, register_generated_file\n",
)
replace_once(
    "services/report_formatted.py",
    '''    filename = f"整形シート_{project.name}_{ts}.xlsx"\n    out_dir = os.path.join(config.OUTPUT_DIR, str(project_id))\n    os.makedirs(out_dir, exist_ok=True)\n    full_path = os.path.join(out_dir, filename)\n    wb.save(full_path)\n\n    rel_path = os.path.join(str(project_id), filename)\n    gf = GeneratedFile(\n        project_id=project_id,\n        file_type="formatted_sheet",\n        file_format="xlsx",\n        original_filename=filename,\n        stored_path=rel_path,\n    )\n    db.session.add(gf)\n    db.session.commit()\n    return gf\n''',
    '''    filename = f"整形シート_{project.name}_{ts}.xlsx"\n    target = prepare_output_target(project_id, filename)\n    wb.save(target.full_path)\n    return register_generated_file(\n        target,\n        project_id=project_id,\n        file_type="formatted_sheet",\n        file_format="xlsx",\n    )\n''',
)

# analysis xlsx/csv
replace_once(
    "services/report_analysis.py",
    "from models import db\n",
    "from models import db\n"
    "from services.file_manager import prepare_output_target, register_generated_file\n",
)
replace_once(
    "services/report_analysis.py",
    '''    filename = f"分析データ_{project.name}_{ts}.xlsx"\n    out_dir  = os.path.join(config.OUTPUT_DIR, str(project_id))\n    os.makedirs(out_dir, exist_ok=True)\n    full_path = os.path.join(out_dir, filename)\n    wb.save(full_path)\n\n    rel_path = os.path.join(str(project_id), filename)\n    gf = GeneratedFile(\n        project_id=project_id,\n        file_type="analysis",\n        file_format="xlsx",\n        original_filename=filename,\n        stored_path=rel_path,\n    )\n    db.session.add(gf)\n    db.session.commit()\n    return gf\n''',
    '''    filename = f"分析データ_{project.name}_{ts}.xlsx"\n    target = prepare_output_target(project_id, filename)\n    wb.save(target.full_path)\n    return register_generated_file(\n        target,\n        project_id=project_id,\n        file_type="analysis",\n        file_format="xlsx",\n    )\n''',
)
replace_once(
    "services/report_analysis.py",
    '''    filename = f"分析データ_{project.name}_{ts}.csv"\n    out_dir  = os.path.join(config.OUTPUT_DIR, str(project_id))\n    os.makedirs(out_dir, exist_ok=True)\n    full_path = os.path.join(out_dir, filename)\n\n    with open(full_path, "w", encoding="utf-8-sig", newline="") as f:\n        writer = csv.writer(f)\n        writer.writerows(rows)\n\n    rel_path = os.path.join(str(project_id), filename)\n    gf = GeneratedFile(\n        project_id=project_id,\n        file_type="analysis",\n        file_format="csv",\n        original_filename=filename,\n        stored_path=rel_path,\n    )\n    db.session.add(gf)\n    db.session.commit()\n    return gf\n''',
    '''    filename = f"分析データ_{project.name}_{ts}.csv"\n    target = prepare_output_target(project_id, filename)\n\n    with open(target.full_path, "w", encoding="utf-8-sig", newline="") as f:\n        writer = csv.writer(f)\n        writer.writerows(rows)\n\n    return register_generated_file(\n        target,\n        project_id=project_id,\n        file_type="analysis",\n        file_format="csv",\n    )\n''',
)

# approved analysis
replace_once(
    "services/report_approved_analysis.py",
    "from models.project import Project\n",
    "from models.project import Project\n"
    "from services.file_manager import prepare_output_target, register_generated_file\n",
)
replace_once(
    "services/report_approved_analysis.py",
    '''    filename = f"承認済AI分析_{project.name}_{ts}.xlsx"\n    out_dir = os.path.join(config.OUTPUT_DIR, str(project_id))\n    os.makedirs(out_dir, exist_ok=True)\n    full_path = os.path.join(out_dir, filename)\n    wb.save(full_path)\n\n    rel_path = os.path.join(str(project_id), filename)\n    params = {\n        "approved_only": True,\n        "analysis_count": len(summary_rows) - 1,\n        "finding_count": finding_count,\n    }\n    gf = GeneratedFile(\n        project_id=project_id,\n        file_type="approved_analysis",\n        file_format="xlsx",\n        original_filename=filename,\n        stored_path=rel_path,\n        generation_params_json=json.dumps(params, ensure_ascii=False),\n    )\n    db.session.add(gf)\n    db.session.commit()\n    return gf\n''',
    '''    filename = f"承認済AI分析_{project.name}_{ts}.xlsx"\n    target = prepare_output_target(project_id, filename)\n    wb.save(target.full_path)\n\n    params = {\n        "approved_only": True,\n        "analysis_count": len(summary_rows) - 1,\n        "finding_count": finding_count,\n    }\n    return register_generated_file(\n        target,\n        project_id=project_id,\n        file_type="approved_analysis",\n        file_format="xlsx",\n        generation_params_json=json.dumps(params, ensure_ascii=False),\n    )\n''',
)

# download path validation
replace_once(
    "routes/outputs.py",
    "from services.report_approved_analysis import generate_approved_analysis_xlsx\n",
    "from services.report_approved_analysis import generate_approved_analysis_xlsx\n"
    "from services.file_manager import get_full_path\n",
)
replace_once(
    "routes/outputs.py",
    '''    gf = GeneratedFile.query.get_or_404(file_id)\n    full_path = os.path.join(config.OUTPUT_DIR, gf.stored_path)\n    if not os.path.isfile(full_path):\n        abort(404)\n''',
    '''    gf = GeneratedFile.query.get_or_404(file_id)\n    try:\n        full_path = get_full_path(gf)\n    except ValueError:\n        abort(404)\n    if not os.path.isfile(full_path):\n        abort(404)\n''',
)
