# Worker launch fencing

Durable `ProcessingJob` launch has two independent actors: the Flask parent that spawns the detached process and the child worker that atomically claims `pending -> running`. Parent-side launch bookkeeping must never overwrite a state already owned by the child.

## Contract

`services.worker_launch_guard.launch_job_or_preserve_active()` is the HTTP-route launch boundary.

A launcher exception may mark a job `failed` only when the exact admitted row is still:

- `status='pending'`;
- on the same observed `attempt_count`; and
- `worker_pid IS NULL`.

That compare-and-swap identifies a definite pre-spawn/unclaimed failure.

`worker_pid=0` is the launch-reservation sentinel used by `launch_job_worker()`. A non-null PID field, including that sentinel, means process creation may already have crossed the spawn boundary. Likewise, `running` or `succeeded` means the child has advanced durable state. In those cases a parent-side exception is treated as bookkeeping ambiguity, not authority to write `failed`.

The child remains authoritative for `pending -> running`: `_claim_pending_job()` increments `attempt_count` and attaches its real PID atomically. If the parent fails after reserving launch but before recording the PID, the child can still replace the sentinel and claim the job. If no child ever claims, existing stale-job recovery handles the abandoned active row after its grace/liveness checks.

Both `routes/transcribe.py` and `routes/analysis_view.py` use this shared boundary. No HTTP worker-launch route should directly convert an arbitrary `launch_job_worker()` exception into an unconditional `ProcessingJob.status='failed'` write.

## Regression

`tests/smoke_worker_launch_fencing.py` uses a temporary SQLite database and no external providers. It proves:

- a definite pre-spawn failure still marks the untouched pending job failed;
- a pending job with the launch-reservation sentinel is not overwritten failed by parent bookkeeping failure;
- the child can subsequently claim that reserved job and attach its PID/attempt token;
- a child that already claimed `running` cannot be overwritten by a later parent launcher exception; and
- both transcription/mapping/project-pipeline routes and project-analysis routes use the shared launch guard.

`.github/workflows/worker-launch-fencing.yml` runs this regression on both Windows and Ubuntu.