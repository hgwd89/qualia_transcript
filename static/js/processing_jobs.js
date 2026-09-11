(() => {
  function interviewIdFromPath() {
    const match = window.location.pathname.match(/^\/interviews\/(\d+)(?:\/|$)/);
    return match ? Number(match[1]) : null;
  }

  function message(text, type = 'info') {
    if (typeof window.showMsg === 'function') {
      window.showMsg(text, type);
      return;
    }
    const el = document.getElementById('status-msg');
    if (!el) return;
    el.className = `mb-4 p-3 rounded text-sm flash-${type}`;
    el.textContent = text;
    el.classList.remove('hidden');
  }

  async function post(url) {
    const response = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    return data;
  }

  function buttonForJobType(jobType) {
    return {
      transcribe: 'btn-transcribe',
      map: 'btn-map',
      analyze: 'btn-analyze',
    }[jobType] || null;
  }

  function setButtonDisabled(jobType, disabled) {
    const id = buttonForJobType(jobType);
    if (!id) return;
    const button = document.getElementById(id);
    if (button) button.disabled = disabled;
  }

  function stageLabel(job) {
    const stage = job.progress && job.progress.stage ? job.progress.stage : job.status;
    const labels = {
      queued: '待機中',
      retry_queued: '再実行待機中',
      worker_started: 'worker起動済み',
      transcribing: '文字起こし中',
      mapping: 'AIマッピング中',
      analyzing: 'AI考察生成中',
      project_pipeline: '一括処理中',
      completed: '完了',
      failed: '失敗',
      pending: '待機中',
      running: '処理中',
    };
    let label = labels[stage] || stage;
    if (job.progress && job.progress.current && job.progress.total) {
      label += ` (${job.progress.current}/${job.progress.total})`;
    }
    return label;
  }

  async function pollJob(jobId, jobType) {
    setButtonDisabled(jobType, true);
    for (;;) {
      let data;
      try {
        const response = await fetch(`/api/processing-jobs/${jobId}`, {cache: 'no-store'});
        data = await response.json();
        if (!response.ok || !data.ok || !data.job) {
          throw new Error(data.error || `HTTP ${response.status}`);
        }
      } catch (error) {
        message(`ジョブ状態の取得に失敗: ${error.message}`, 'error');
        setButtonDisabled(jobType, false);
        return;
      }

      const job = data.job;
      if (job.status === 'succeeded') {
        message(`${stageLabel(job)}。画面を更新します。`, 'success');
        window.setTimeout(() => window.location.reload(), 700);
        return;
      }
      if (job.status === 'failed') {
        message(`処理失敗: ${job.error_message || '詳細不明'}。再実行できます。`, 'error');
        setButtonDisabled(jobType, false);
        return;
      }

      message(`ジョブ #${job.id}: ${stageLabel(job)}`, 'info');
      await new Promise(resolve => window.setTimeout(resolve, 1500));
    }
  }

  async function enqueue(path, jobType, initialMessage) {
    setButtonDisabled(jobType, true);
    message(initialMessage, 'info');
    try {
      const data = await post(path);
      if (data.already_done) {
        message(`既に完了しています。${data.segment_count != null ? ` ${data.segment_count} セグメント` : ''}`, 'success');
        window.setTimeout(() => window.location.reload(), 700);
        return;
      }
      if (!data.job_id) {
        throw new Error('job_id が返されませんでした');
      }
      message(`ジョブ #${data.job_id} を受け付けました。`, 'info');
      await pollJob(data.job_id, jobType);
    } catch (error) {
      message(`エラー: ${error.message}`, 'error');
      setButtonDisabled(jobType, false);
    }
  }

  function applyCanonicalRoles(segmentRoles) {
    for (const item of segmentRoles || []) {
      const row = document.getElementById(`segment-${item.segment_id}`);
      const select = row ? row.querySelector('select') : null;
      if (!select) continue;

      let value = item.speaker_role || 'unknown';
      if (value === 'moderator') value = 'interviewer';
      if (value === 'respondent' && item.participant_id != null) {
        value = `respondent_${item.participant_id}`;
      }
      if ([...select.options].some(option => option.value === value)) {
        select.value = value;
      }
    }
  }

  const interviewId = interviewIdFromPath();
  if (!interviewId) return;

  window.runTranscribe = () => enqueue(
    `/api/interviews/${interviewId}/transcribe`,
    'transcribe',
    '文字起こしジョブを登録中…',
  );
  window.runMapping = () => enqueue(
    `/api/interviews/${interviewId}/map`,
    'map',
    'AIマッピングジョブを登録中…',
  );
  window.runAnalyze = () => enqueue(
    `/api/interviews/${interviewId}/analyze`,
    'analyze',
    'AI考察ジョブを登録中…',
  );

  // The legacy template labels the moderator as "interviewer". Keep that
  // presentation label while persisting the canonical backend role "moderator".
  window.updateRole = async (segId, select) => {
    const raw = select.value;
    const body = raw.startsWith('respondent_')
      ? {speaker_role: 'respondent', participant_id: Number(raw.split('_')[1])}
      : {speaker_role: raw === 'interviewer' ? 'moderator' : raw, participant_id: null};

    const response = await fetch(`/interviews/${interviewId}/segments/${segId}/role`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      message(`話者更新に失敗: ${data.error || `HTTP ${response.status}`}`, 'error');
    }
  };

  window.addEventListener('DOMContentLoaded', async () => {
    try {
      const response = await fetch(`/api/interviews/${interviewId}/status`, {cache: 'no-store'});
      const data = await response.json();
      applyCanonicalRoles(data.segment_roles);
      const job = data.latest_job;
      if (job && (job.status === 'pending' || job.status === 'running')) {
        pollJob(job.id, job.job_type);
      }
    } catch (_) {
      // Status restoration is best-effort. Manual actions remain available.
    }
  });
})();
