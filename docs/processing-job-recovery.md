# Processing job recovery

Qualia Transcript stores long-running transcription, mapping, analysis, and project-pipeline work in `processing_jobs`.

## Automatic recovery

Before a new conflicting job is accepted, the app checks active jobs in the same project.

An active job is automatically moved to `failed` when either condition is true:

- `pending` has no worker PID for more than 5 minutes.
- `pending` or `running` has remained active for more than 12 hours.

The conservative 12-hour ceiling avoids treating a legitimate long transcription as dead while ensuring a crashed/rebooted worker cannot block the project forever. Recovered jobs retain an error message beginning with `job recovery:` and can be retried through the normal job retry path.

## Immediate operator recovery

If the app/worker was explicitly stopped or the PC restarted and you do not want to wait for the automatic threshold, inspect the job first:

```powershell
python scripts/recover_processing_job.py --job-id 123
```

The command is validation-only by default and does not change the database.

After you have confirmed the worker is no longer running, explicitly mark the job failed:

```powershell
python scripts/recover_processing_job.py --job-id 123 --apply --yes
```

The job can then be retried from the normal API/UI flow.

## Safety rule

Do not force-recover a job while its worker is still processing. Doing so removes the serialization guard and could allow duplicate transcription/mapping/analysis work against the same interview.
