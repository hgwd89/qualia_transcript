# Retry recovery grace

Automatic stale-job recovery must measure the no-worker grace period from the current active attempt, not permanently from the original `ProcessingJob.created_at`.

## Problem

A durable job row is reused when a failed job is retried. Its `created_at` is historical identity and can therefore be hours or days old. Before this contract, `stale_reason()` used only `created_at` when `worker_pid` was null or the launch-reservation sentinel `0` was present. A retry of any job older than the five-minute startup grace could consequently be marked `recovered_failed` immediately after admission, before the launcher/child had a fair chance to claim the new attempt.

## Contract

- `created_at` remains immutable historical job creation time.
- Retry admission sets `started_at` to the retry admission time while the job is pending. During this short pending period it is the grace anchor for the current active attempt.
- `_claim_pending_job()` overwrites `started_at` with the actual worker-claim time, restoring its normal running-attempt meaning.
- `stale_reason()` uses `started_at` first and falls back to `created_at` only when no current-attempt anchor exists, as with a first-time pending job.
- `worker_pid=0` remains a no-worker launch-reservation sentinel, but it receives the same current-attempt grace instead of inheriting the original job age.
- Once the fresh grace expires, an unclaimed retry remains recoverable exactly like any other abandoned active job.

This preserves historical creation time while preventing stale recovery from racing a legitimate retry admission/launch.

## Regression

`tests/smoke_retry_recovery_grace.py` uses a temporary SQLite database and no external providers. It verifies that an hours-old failed job receives a fresh retry anchor, survives immediate recovery and launch reservation, becomes stale after the new grace expires, and has its anchor replaced by actual worker claim. It also verifies that first-time pending jobs still fall back to `created_at`.

`.github/workflows/retry-recovery-grace.yml` runs this regression on both Windows and Ubuntu.