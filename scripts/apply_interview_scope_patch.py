from pathlib import Path

path = Path("routes/interviews.py")
text = path.read_text(encoding="utf-8")
old = '''    if request.method == "POST":\n        date_str = request.form.get("interview_date", "").strip()\n        interview = Interview(\n            project_id=project_id,\n            participant_id=request.form.get("participant_id") or None,\n            flow_id=request.form.get("flow_id") or None,\n'''
new = '''    if request.method == "POST":\n        date_str = request.form.get("interview_date", "").strip()\n        participant_id = request.form.get("participant_id") or None\n        flow_id = request.form.get("flow_id") or None\n        if participant_id is not None:\n            participant = (\n                Participant.query\n                .filter_by(id=participant_id, project_id=project_id)\n                .first_or_404()\n            )\n            participant_id = participant.id\n        if flow_id is not None:\n            flow = (\n                InterviewFlow.query\n                .filter_by(id=flow_id, project_id=project_id)\n                .first_or_404()\n            )\n            flow_id = flow.id\n\n        interview = Interview(\n            project_id=project_id,\n            participant_id=participant_id,\n            flow_id=flow_id,\n'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one interview-new block, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("interview project-scope patch applied")
