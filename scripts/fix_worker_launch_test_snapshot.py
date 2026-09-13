from pathlib import Path

path = Path("tests/smoke_worker_launch_fencing.py")
text = path.read_text(encoding="utf-8")
old = '''                    db.session.expire_all()
                    after_stale = db.session.get(ProcessingJob, aba_id)
                    fresh_child, fresh_claimed = processing_jobs._claim_pending_job(
                        aba_id,
                        worker_pid=44002,
                        expected_attempt_count=second_attempt,
                        expected_started_at=second_anchor,
                    )
                    failures += check(
                        "stale parent and child cannot cross a reused launch reservation generation",
                        first_retry.created
                        and second_retry.created
                        and first_reserved
                        and second_reserved
                        and first_anchor is not None
                        and second_anchor is not None
                        and first_anchor != second_anchor
                        and not stale_recorded
                        and not stale_released
                        and not stale_claimed
                        and after_stale.status == "pending"
                        and int(after_stale.worker_pid or 0) == 0
                        and after_stale.started_at == second_anchor
                        and fresh_claimed
'''
new = '''                    db.session.expire_all()
                    after_stale = db.session.get(ProcessingJob, aba_id)
                    after_stale_status = after_stale.status
                    after_stale_pid = after_stale.worker_pid
                    after_stale_anchor = after_stale.started_at
                    fresh_child, fresh_claimed = processing_jobs._claim_pending_job(
                        aba_id,
                        worker_pid=44002,
                        expected_attempt_count=second_attempt,
                        expected_started_at=second_anchor,
                    )
                    failures += check(
                        "stale parent and child cannot cross a reused launch reservation generation",
                        first_retry.created
                        and second_retry.created
                        and first_reserved
                        and second_reserved
                        and first_anchor is not None
                        and second_anchor is not None
                        and first_anchor != second_anchor
                        and not stale_recorded
                        and not stale_released
                        and not stale_claimed
                        and after_stale_status == "pending"
                        and int(after_stale_pid or 0) == 0
                        and after_stale_anchor == second_anchor
                        and fresh_claimed
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected generated test anchor once, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("worker launch ABA test snapshot repaired")
