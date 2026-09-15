# Operator recovery generation fencing

Manual processing-job recovery is an administrative write against live canonical state. The operator first inspects a durable job row, then may choose `--apply --yes`. Workers, launchers, and retries remain concurrent during that interval, so job ID alone is not a sufficient recovery target.

The recovery target is the exact inspected generation:

- durable row identity: `id` plus immutable `created_at`
- active state: `status`
- retry/claim generation: `attempt_count` and `started_at`
- worker ownership: `worker_pid`

The CLI carries this token from the read-only inspection into the write path. If the live row no longer matches, the write is refused and the operator must inspect again. If it still matches, the service performs the same generation-sensitive compare-and-swap during the database update. This closes both the inspection-to-write TOCTOU window and the final check-to-commit window.

This fence intentionally does not prevent an operator from force-failing the exact generation they inspected after independent confirmation that its worker should no longer own the job. It prevents that decision from silently applying to a later generation.
