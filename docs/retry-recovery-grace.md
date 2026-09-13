# Retry recovery grace

Automatic stale-job recovery must measure the no-worker grace period from the current active attempt, not permanently from the original `ProcessingJob.created_at`.

## Problem

A durable job row is reused when a failed job is retried. Its `created_at` is historical identity and can therefore be hours or days old. Before this contract, stale recovery used only `created_at` when `worker_pid` was null or the launch-reservation sentinel `0` was present. A retry of any job older than the five-minute startup grace could consequently be marked `recovered_failed` immediately after admission, before the launcher/child had a fair chance to claim the new attempt.

The same row reuse creates an ABA risk during recovery. Worker claim is the point where `attempt_count` increments. Before that claim, one retry may fail and a second retry may return the row to exactly the same `pending`, unchanged `attempt_count`, and `worker_pid=None` shape. A recovery decision based on the first retry must not be allowed to compare-and-swap the second retry merely because those older fields match.

The manual `scripts/recover_processing_job.py` inspection path must use the same attempt anchor. Otherwise an operator can be shown an `automatic_recovery_reason` that disagrees with the application's actual automatic-recovery decision for a fresh retry.

## Contract

- `created_at` remains immutable historical job creation time.
- Retry admission sets `started_at` to the retry admission time while the job is pending. During this short pending period it is the grace anchor and identity token for the current active attempt.
- `_claim_pending_job()` overwrites `started_at` with the actual worker-claim time, restoring its normal running-attempt meaning.
- `services.job_recovery.stale_reason()` uses `started_at` first and falls back to `created_at` only when no current-attempt anchor exists, as with a first-time pending job.
- Recovery compare-and-swap updates include `started_at` in addition to `status`, `attempt_count`, and `worker_pid`. A stale observation from an earlier retry therefore cannot fail or complete a later retry that reused the same durable row before worker claim.
- The read-only recovery CLI applies the same `started_at`-first rule when displaying `automatic_recovery_reason`.
- `worker_pid=0` remains a no-worker launch-reservation sentinel, but it receives the same current-attempt grace instead of inheriting the original job age.
- Once the fresh grace expires, an unclaimed retry remains recoverable exactly like any other abandoned active job.

This preserves historical creation time while preventing stale recovery—or its operator-facing diagnosis—from racing a legitimate retry admission/launch.

## Regression

`tests/smoke_retry_recovery_grace.py` uses a temporary SQLite database and no external providers. It verifies that an hours-old failed job receives a fresh retry anchor, survives immediate recovery and launch reservation, becomes stale after the new grace expires, and has its anchor replaced by actual worker claim. It also reproduces a pre-claim `failed -> pending -> failed -> pending` ABA on one durable row and proves that a recovery CAS captured from the first retry cannot overwrite the second retry even though status, attempt count, and worker PID are identical. First-time pending jobs still fall back to `created_at`.

`tests/smoke_job_recovery.py` additionally verifies that the read-only recovery CLI reports the same current-attempt grace semantics without mutating the inspected database.

`.github/workflows/retry-recovery-grace.yml` and the durable job recovery regression workflow exercise these contracts on Windows and Ubuntu.
