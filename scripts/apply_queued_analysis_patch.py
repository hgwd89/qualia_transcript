from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if new in text:
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}\n--- old ---\n{old}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# Durable processing job types + handlers.
path = "services/processing_jobs.py"
replace_once(
    path,
    'JOB_TYPES = {"transcribe", "map", "analyze", "project_pipeline"}\n',
    'JOB_TYPES = {"transcribe", "map", "analyze", "analyze_question", "analyze_cross", "analyze_integrated", "project_pipeline"}\n',
)
replace_once(
    path,
    '''def create_or_get_active_job(project_id: int, job_type: str, interview_id: int | None = None):
    if job_type not in JOB_TYPES:
        raise ValueError(f"unsupported job_type: {job_type}")

    query = ProcessingJob.query.filter_by(
        project_id=project_id,
        interview_id=interview_id,
        job_type=job_type,
    ).filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
    existing = query.order_by(ProcessingJob.id.desc()).first()
    if existing:
        return existing, False

    job = ProcessingJob(
        project_id=project_id,
        interview_id=interview_id,
        job_type=job_type,
        status="pending",
        progress_json=_json_dump({"stage": "queued"}),
    )
    db.session.add(job)
    db.session.commit()
    return job, True
''',
    '''def create_or_get_active_job(
    project_id: int,
    job_type: str,
    interview_id: int | None = None,
    *,
    question_id: int | None = None,
):
    if job_type not in JOB_TYPES:
        raise ValueError(f"unsupported job_type: {job_type}")

    query = ProcessingJob.query.filter_by(
        project_id=project_id,
        interview_id=interview_id,
        question_id=question_id,
        job_type=job_type,
    ).filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
    existing = query.order_by(ProcessingJob.id.desc()).first()
    if existing:
        return existing, False

    job = ProcessingJob(
        project_id=project_id,
        interview_id=interview_id,
        question_id=question_id,
        job_type=job_type,
        status="pending",
        progress_json=_json_dump({"stage": "queued"}),
    )
    db.session.add(job)
    db.session.commit()
    return job, True
''',
)
replace_once(
    path,
    '''def _perform_project_pipeline(job: ProcessingJob) -> dict:
    from services.project_pipeline import run_project_pipeline

    return run_project_pipeline(job, update_progress)
''',
    '''def _perform_question_analysis(job: ProcessingJob) -> dict:
    from models.interview import Interview
    from models.interview_flow import InterviewFlowQuestion
    from services.analyzer import analyze_per_question
    from services.processing_result_guard import find_completed_analysis_for_scope

    if job.interview_id is None or job.question_id is None:
        raise ValueError("question analysis job requires interview_id and question_id")
    interview = db.session.get(Interview, int(job.interview_id))
    question = db.session.get(InterviewFlowQuestion, int(job.question_id))
    if not interview or interview.project_id != job.project_id:
        raise ValueError("interview not found in job project")
    if not question or not interview.flow_id or question.section.flow_id != interview.flow_id:
        raise ValueError("question not found in interview flow")

    existing = find_completed_analysis_for_scope(job, "per_question")
    if existing:
        update_progress(job, "analyzing_question", analysis_id=existing.id, already_done=True)
        return {"analysis_id": existing.id, "already_done": True}

    update_progress(job, "analyzing_question", question_id=int(job.question_id))
    analysis = analyze_per_question(
        int(job.interview_id),
        int(job.question_id),
        result_write_guard=lambda: begin_job_result_write(job),
    )
    return {"analysis_id": analysis.id, "question_id": int(job.question_id)}


def _perform_cross_analysis(job: ProcessingJob) -> dict:
    from models.interview_flow import InterviewFlowQuestion
    from services.analyzer import analyze_cross_participants
    from services.processing_result_guard import find_completed_analysis_for_scope

    if job.question_id is None:
        raise ValueError("cross analysis job requires question_id")
    question = db.session.get(InterviewFlowQuestion, int(job.question_id))
    if not question or not question.section or not question.section.flow:
        raise ValueError("question not found")
    if question.section.flow.project_id != job.project_id:
        raise ValueError("question not found in job project")

    existing = find_completed_analysis_for_scope(job, "cross_participant")
    if existing:
        update_progress(job, "analyzing_cross", analysis_id=existing.id, already_done=True)
        return {"analysis_id": existing.id, "already_done": True}

    update_progress(job, "analyzing_cross", question_id=int(job.question_id))
    analysis = analyze_cross_participants(
        int(job.project_id),
        int(job.question_id),
        result_write_guard=lambda: begin_job_result_write(job),
    )
    return {"analysis_id": analysis.id, "question_id": int(job.question_id)}


def _perform_integrated_analysis(job: ProcessingJob) -> dict:
    from models.project import Project
    from services.analyzer import analyze_project_integrated
    from services.processing_result_guard import find_completed_analysis_for_scope

    project = db.session.get(Project, int(job.project_id))
    if not project:
        raise ValueError("project not found")

    existing = find_completed_analysis_for_scope(job, "integrated")
    if existing:
        update_progress(job, "analyzing_integrated", analysis_id=existing.id, already_done=True)
        return {"analysis_id": existing.id, "already_done": True}

    update_progress(job, "analyzing_integrated")
    analysis = analyze_project_integrated(
        int(job.project_id),
        result_write_guard=lambda: begin_job_result_write(job),
    )
    return {"analysis_id": analysis.id}


def _perform_project_pipeline(job: ProcessingJob) -> dict:
    from services.project_pipeline import run_project_pipeline

    return run_project_pipeline(job, update_progress)
''',
)
replace_once(
    path,
    '''    default_handlers = {
        "transcribe": _perform_transcription,
        "map": _perform_mapping,
        "analyze": _perform_analysis,
        "project_pipeline": _perform_project_pipeline,
    }
''',
    '''    default_handlers = {
        "transcribe": _perform_transcription,
        "map": _perform_mapping,
        "analyze": _perform_analysis,
        "analyze_question": _perform_question_analysis,
        "analyze_cross": _perform_cross_analysis,
        "analyze_integrated": _perform_integrated_analysis,
        "project_pipeline": _perform_project_pipeline,
    }
''',
)

# Fence canonical writes in the three remaining AI analysis services.
path = "services/analyzer.py"
replace_once(
    path,
    'def analyze_per_question(interview_id: int, question_id: int) -> AIAnalysis:\n    interview = Interview.query.get(interview_id)\n    question  = InterviewFlowQuestion.query.get(question_id)\n',
    '''def analyze_per_question(
    interview_id: int,
    question_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> AIAnalysis:
    interview = Interview.query.get(interview_id)
    question = InterviewFlowQuestion.query.get(question_id)
    if not interview or not question:
        raise ValueError("interview または question が見つかりません")
''',
)
replace_once(
    path,
    '''    normalized_result = {
        "question_id": question.id,
        "question_code": canonical_q_code,
        "question_text": question.question_text,
        "findings": normalized_findings,
        "implications": result.get("implications", ""),
        "unresolved": result.get("unresolved", ""),
    }

    analysis = AIAnalysis(
''',
    '''    normalized_result = {
        "question_id": question.id,
        "question_code": canonical_q_code,
        "question_text": question.question_text,
        "findings": normalized_findings,
        "implications": result.get("implications", ""),
        "unresolved": result.get("unresolved", ""),
    }

    if result_write_guard is not None:
        result_write_guard()

    analysis = AIAnalysis(
''',
)
replace_once(
    path,
    'def analyze_cross_participants(project_id: int, question_id: int) -> AIAnalysis:\n',
    '''def analyze_cross_participants(
    project_id: int,
    question_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> AIAnalysis:
''',
)
replace_once(
    path,
    '''    result = call_structured(system, user, CROSS_SCHEMA, schema_name="cross_analysis_result")

    analysis = AIAnalysis(
''',
    '''    result = call_structured(system, user, CROSS_SCHEMA, schema_name="cross_analysis_result")

    if result_write_guard is not None:
        result_write_guard()

    analysis = AIAnalysis(
''',
)
replace_once(
    path,
    'def analyze_project_integrated(project_id: int) -> AIAnalysis:\n',
    '''def analyze_project_integrated(
    project_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> AIAnalysis:
''',
)
replace_once(
    path,
    '''    result = call_structured(system, user, INTEGRATED_SCHEMA, schema_name="integrated_result")

    analysis = AIAnalysis(
''',
    '''    result = call_structured(system, user, INTEGRATED_SCHEMA, schema_name="integrated_result")

    if result_write_guard is not None:
        result_write_guard()

    analysis = AIAnalysis(
''',
)

# Analysis page: queue -> poll -> resume rather than treating admission as completion.
path = "templates/analysis/index.html"
replace_once(
    path,
    '''async function runCross(questionId, btn){
  btn.disabled = true;
  btn.textContent = '実行中…';
  showMsg('横断分析を実行中です（数分かかる場合があります）…', 'info');
  try {
    const r = await fetch(`/api/projects/${projectId}/analyze/cross/${questionId}`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:'{}',
    }).then(r => r.json());
    if(r.ok){
      showMsg('横断分析が完了しました', 'success');
      setTimeout(() => location.reload(), 1500);
    } else {
      showMsg('エラー: ' + (r.error || '不明'), 'error');
      btn.disabled = false;
      btn.textContent = btn.getAttribute('data-original') || '横断分析';
    }
  } catch(e){
    showMsg('通信エラー: ' + e.message, 'error');
    btn.disabled = false;
  }
}

async function runIntegrated(){
  const btn = document.getElementById('btn-integrated');
  btn.disabled = true;
  btn.textContent = '実行中…';
  showMsg('統合分析を実行中です（数分かかる場合があります）…', 'info');
  try {
    const r = await fetch(`/api/projects/${projectId}/analyze/integrated`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:'{}',
    }).then(r => r.json());
    if(r.ok){
      showMsg('統合分析が完了しました', 'success');
      setTimeout(() => location.reload(), 1500);
    } else {
      showMsg('エラー: ' + (r.error || '不明'), 'error');
      btn.disabled = false;
      btn.textContent = '統合分析を実行';
    }
  } catch(e){
    showMsg('通信エラー: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = '統合分析を実行';
  }
}
''',
    '''function rememberButtonText(btn){
  if(btn && !btn.dataset.originalText){
    btn.dataset.originalText = btn.textContent.trim();
  }
}

function setAnalysisButtonBusy(btn, busy){
  if(!btn) return;
  rememberButtonText(btn);
  btn.disabled = busy;
  btn.textContent = busy ? '実行中…' : (btn.dataset.originalText || '分析を実行');
}

async function enqueueAnalysis(url){
  const response = await fetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:'{}',
  });
  const data = await response.json().catch(() => ({}));
  if(!response.ok || !data.ok){
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  if(!data.queued || !data.job_id){
    throw new Error('analysis job_id が返されませんでした');
  }
  return data;
}

async function pollAnalysisJob(jobId, btn, runningLabel){
  setAnalysisButtonBusy(btn, true);
  for(;;){
    const response = await fetch(`/api/processing-jobs/${jobId}`, {cache:'no-store'});
    const data = await response.json().catch(() => ({}));
    if(!response.ok || !data.ok || !data.job){
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    const job = data.job;
    if(job.status === 'succeeded') return job;
    if(job.status === 'failed') throw new Error(job.error_message || '分析ジョブが失敗しました');
    showMsg(`${runningLabel} ジョブ #${jobId}`, 'info');
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
}

async function runCross(questionId, btn){
  setAnalysisButtonBusy(btn, true);
  showMsg('横断分析ジョブを登録中です…', 'info');
  try {
    const queued = await enqueueAnalysis(`/api/projects/${projectId}/analyze/cross/${questionId}`);
    await pollAnalysisJob(queued.job_id, btn, '横断分析中…');
    showMsg('横断分析が完了しました', 'success');
    setTimeout(() => location.reload(), 800);
  } catch(e){
    showMsg('エラー: ' + e.message, 'error');
    setAnalysisButtonBusy(btn, false);
  }
}

async function runIntegrated(){
  const btn = document.getElementById('btn-integrated');
  setAnalysisButtonBusy(btn, true);
  showMsg('統合分析ジョブを登録中です…', 'info');
  try {
    const queued = await enqueueAnalysis(`/api/projects/${projectId}/analyze/integrated`);
    await pollAnalysisJob(queued.job_id, btn, '統合分析中…');
    showMsg('統合分析が完了しました', 'success');
    setTimeout(() => location.reload(), 800);
  } catch(e){
    showMsg('エラー: ' + e.message, 'error');
    setAnalysisButtonBusy(btn, false);
  }
}

async function resumeActiveAnalysisJob(){
  try {
    const response = await fetch(`/api/projects/${projectId}/analysis-processing-status`, {cache:'no-store'});
    const data = await response.json().catch(() => ({}));
    if(!response.ok || !data.ok || !data.latest_job) return;
    const job = data.latest_job;
    if(job.status !== 'pending' && job.status !== 'running') return;
    let btn = null;
    let label = '分析中…';
    if(job.job_type === 'analyze_cross' && job.question_id){
      btn = document.querySelector(`[data-qid="${job.question_id}"]`);
      label = '横断分析中…';
    } else if(job.job_type === 'analyze_integrated'){
      btn = document.getElementById('btn-integrated');
      label = '統合分析中…';
    } else {
      return;
    }
    await pollAnalysisJob(job.id, btn, label);
    showMsg('分析が完了しました', 'success');
    setTimeout(() => location.reload(), 800);
  } catch(e){
    showMsg('分析ジョブ状態の取得に失敗しました: ' + e.message, 'error');
  }
}

document.addEventListener('DOMContentLoaded', resumeActiveAnalysisJob);
''',
)

print("queued analysis source patches applied")
