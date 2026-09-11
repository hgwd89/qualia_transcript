# Processing job recovery

Qualia Transcript stores long-running transcription, mapping, analysis, and project-pipeline work in `processing_jobs`.

## Automatic recovery

Before a new conflicting job is accepted, the app checks active jobs in the same project.

An active `pending` or `running` job is automatically moved to `failed` only when it still has no worker PID more than 5 minutes after creation.

The 5-minute grace covers launcher/worker failures without racing normal startup. A PID-backed active job is deliberately never failed merely because it is old: without a trustworthy worker-identity/liveness signal, age-based recovery could incorrectly release a legitimate long-running operation and allow duplicate writes. Recovered no-worker jobs retain an error message beginning with `job recovery:` and can be retried through the normal job retry path.

## Immediate operator recovery

If the app/worker was explicitly stopped or the PC restarted and the row still contains a worker PID, inspect the job first:

```powershell
python scripts/recover_processing_job.py --job-id 123
```

The inspection path opens SQLite with `mode=ro`. It does not call the application factory, run migrations, create tables, or change job state. PID-backed active jobs include an operator note explaining that they require explicit confirmation before recovery.

After you have confirmed the worker is no longer running, explicitly mark the job failed:

```powershell
python scripts/recover_processing_job.py --job-id 123 --apply --yes
```

The job can then be retried from the normal API/UI flow.

## Safety rule

Do not force-recover a job while its worker is still processing. Doing so removes the serialization guard and could allow duplicate transcription/mapping/analysis work against the same interview.
