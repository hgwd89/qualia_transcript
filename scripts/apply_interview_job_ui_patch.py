from pathlib import Path

path = Path("templates/interviews/detail.html")
text = path.read_text(encoding="utf-8")
old = '''async function postApi(url){
  const r = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  return r.json();
}

async function runTranscribe(){
  document.getElementById('btn-transcribe').disabled = true;
  showMsg('文字起こし中…しばらくお待ちください', 'info');
  const r = await postApi(`/api/interviews/${interviewId}/transcribe`);
  if(r.ok){
    showMsg(`完了: ${r.segment_count} セグメント生成`, 'success');
    setTimeout(() => location.reload(), 1500);
  } else {
    showMsg('エラー: ' + (r.error || '不明'), 'error');
    document.getElementById('btn-transcribe').disabled = false;
  }
}

async function runMapping(){
  document.getElementById('btn-map').disabled = true;
  showMsg('AIマッピング中…', 'info');
  const r = await postApi(`/api/interviews/${interviewId}/map`);
  if(r.ok){
    showMsg(`完了: ${r.mapped_count} 件マッピングしました`, 'success');
    setTimeout(() => location.reload(), 1500);
  } else {
    showMsg('エラー: ' + (r.error || '不明'), 'error');
    document.getElementById('btn-map').disabled = false;
  }
}

async function runAnalyze(){
  document.getElementById('btn-analyze').disabled = true;
  showMsg('AI考察生成中…（数分かかる場合があります）', 'info');
  const r = await postApi(`/api/interviews/${interviewId}/analyze`);
  if(r.ok){
    showMsg('考察を生成しました', 'success');
    setTimeout(() => location.reload(), 1500);
  } else {
    showMsg('エラー: ' + (r.error || '不明'), 'error');
    document.getElementById('btn-analyze').disabled = false;
  }
}
'''
new = '''async function postApi(url){
  const response = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const data = await response.json().catch(() => ({}));
  if(!response.ok || !data.ok){
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function jobUi(jobType){
  const config = {
    transcribe: {buttonId:'btn-transcribe', running:'文字起こし中…', done:'文字起こしが完了しました'},
    map: {buttonId:'btn-map', running:'AIマッピング中…', done:'マッピングが完了しました'},
    analyze: {buttonId:'btn-analyze', running:'AI考察生成中…', done:'考察生成が完了しました'},
  };
  return config[jobType] || {buttonId:null, running:'処理中…', done:'処理が完了しました'};
}

function setJobButtonDisabled(jobType, disabled){
  const buttonId = jobUi(jobType).buttonId;
  if(!buttonId) return;
  const btn = document.getElementById(buttonId);
  if(btn) btn.disabled = disabled;
}

async function pollInterviewJob(jobId, jobType){
  setJobButtonDisabled(jobType, true);
  const ui = jobUi(jobType);
  for(;;){
    const response = await fetch(`/api/processing-jobs/${jobId}`, {cache:'no-store'});
    const data = await response.json().catch(() => ({}));
    if(!response.ok || !data.ok || !data.job){
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    const job = data.job;
    if(job.status === 'succeeded'){
      return job.result || {};
    }
    if(job.status === 'failed'){
      throw new Error(job.error_message || '処理ジョブが失敗しました');
    }
    showMsg(`${ui.running} ジョブ #${jobId}`, 'info');
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
}

async function runDurableInterviewAction(jobType, url){
  const ui = jobUi(jobType);
  setJobButtonDisabled(jobType, true);
  showMsg(`${ui.running} ジョブを登録中です…`, 'info');
  try {
    const queued = await postApi(url);
    let result = queued;
    if(queued.queued){
      if(!queued.job_id) throw new Error('job_id が返されませんでした');
      showMsg(`${ui.running} ジョブ #${queued.job_id} を受け付けました`, 'info');
      result = await pollInterviewJob(queued.job_id, jobType);
    }
    showMsg(ui.done, 'success');
    setTimeout(() => location.reload(), 800);
    return result;
  } catch(e){
    showMsg('エラー: ' + e.message, 'error');
    setJobButtonDisabled(jobType, false);
    return null;
  }
}

async function runTranscribe(){
  await runDurableInterviewAction('transcribe', `/api/interviews/${interviewId}/transcribe`);
}

async function runMapping(){
  await runDurableInterviewAction('map', `/api/interviews/${interviewId}/map`);
}

async function runAnalyze(){
  await runDurableInterviewAction('analyze', `/api/interviews/${interviewId}/analyze`);
}

async function resumeActiveInterviewJob(){
  try {
    const response = await fetch(`/api/interviews/${interviewId}/status`, {cache:'no-store'});
    const data = await response.json().catch(() => ({}));
    if(!response.ok) return;
    const job = data.latest_job;
    if(!job || (job.status !== 'pending' && job.status !== 'running')) return;
    if(!['transcribe','map','analyze'].includes(job.job_type)) return;
    await pollInterviewJob(job.id, job.job_type);
    showMsg(jobUi(job.job_type).done, 'success');
    setTimeout(() => location.reload(), 800);
  } catch(e){
    showMsg('ジョブ状態の取得に失敗しました: ' + e.message, 'error');
  }
}

document.addEventListener('DOMContentLoaded', resumeActiveInterviewJob);
'''

if new in text:
    print("patch already applied")
    raise SystemExit(0)
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one UI block, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("patched interview detail durable job UI")
