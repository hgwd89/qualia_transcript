# Durable job result-generation recovery

A durable `ProcessingJob` row can survive multiple retries. `created_at` therefore identifies the lifetime of the row, not the generation of one worker attempt.

## Current-attempt result boundary

Every successful `pending -> running` claim refreshes `ProcessingJob.started_at`. Result reconciliation uses that timestamp as the lower bound for artifacts belonging to the current attempt. Legacy rows without `started_at` fall back to `created_at`.

This applies to the canonical artifacts that can be identified without provider calls:

- mapping jobs: replacement `UtteranceMapping` rows created by the current attempt;
- per-participant analysis;
- per-question analysis;
- cross-participant analysis; and
- integrated analysis.

A retry must not adopt an artifact created before its current `started_at`, even when that artifact is newer than the durable job row's original `created_at`.

## Crash-window reconciliation

A worker can commit its domain result and then die before `_finish_job_success()` updates the durable job row. Stale-job recovery checks for a canonical result from the exact current attempt before failing a stale `running` job.

When such a result exists, recovery compare-and-swaps the unchanged `status` / `attempt_count` / `worker_pid` state to `succeeded`, persists the recovered result payload, clears the worker PID, and records `recovered_completed` progress. If no current-attempt result exists, normal stale recovery marks the job `failed`.

The compare-and-swap prevents a recovery decision from overwriting a worker or retry that advanced the durable row after liveness inspection.

Transcription and project-pipeline jobs are not inferred through this generic artifact reconciliation. Their recovery remains governed by their dedicated canonical state and retry paths.

## Regression

`tests/smoke_job_recovery_result_generation.py` uses a temporary SQLite database and no external providers. It proves that current-attempt mapping/analysis artifacts recover a dead worker as succeeded, while an older-attempt artifact is not adopted by a retry generation.

`.github/workflows/job-recovery-result-generation.yml` runs this regression on both Windows and Ubuntu.
