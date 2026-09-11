# Processing job recovery

Qualia Transcript stores long-running transcription, mapping, analysis, and project-pipeline work in `processing_jobs`.

## Automatic recovery

Before a new conflicting job is accepted, and while job/interview status is polled, the app checks active jobs in the same project.

An active `pending` or `running` job is automatically moved to `failed` only when recovery is unambiguous:

- the job still has no worker PID more than 5 minutes after creation, or
- the job has a worker PID and the operating system confirms that PID is no longer running.

A PID-backed job is **not** auto-failed because it is merely old. If PID liveness cannot be determined, the job remains protected. This biases the system against duplicate writes: a false positive “still alive” may require operator recovery, but an uncertain worker is never released automatically.

Recovered jobs retain an error message beginning with `job recovery:` and can be retried through the normal job retry path.

## Immediate operator recovery

Inspect a job first:

```powershell
python scripts/recover_processing_job.py --job-id 123
```

The inspection path opens SQLite with `mode=ro`. It does not call the application factory, run migrations, create tables, or change job state. For active PID-backed jobs it also reports `worker_pid_alive`:

- `true`: the PID is alive; do not force-recover unless you have independently confirmed it is not the Qualia worker for this job.
- `false`: the PID is dead; normal status polling/conflict checks should automatically move the job to `failed`.
- `null`: liveness is inconclusive; keep the job protected until you confirm the worker has stopped.

After you have independently confirmed that an active job's worker is no longer running, explicitly mark the job failed if automatic recovery has not already done so:

```powershell
python scripts/recover_processing_job.py --job-id 123 --apply --yes
```

The job can then be retried from the normal API/UI flow.

## Safety rule

Do not force-recover a job while its worker is still processing. Doing so removes the serialization guard and could allow duplicate transcription/mapping/analysis work against the same interview.
