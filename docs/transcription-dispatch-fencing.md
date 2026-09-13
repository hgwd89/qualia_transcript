# Production transcription dispatch fencing

`services.processing_jobs._perform_transcription()` uses `services.transcription_dispatch.run_transcription()` as the durable production entry point.

That dispatcher must preserve the durable `lease_check` and `result_write_guard` callbacks on every provider path, including direct local Whisper and OpenAI-to-local fallback.

When OpenAI long-audio processing has already committed partial `Segment` rows and then falls back to local Whisper, the dispatcher must acquire the stronger result-write reservation before deleting those partial canonical rows. The local provider must then receive the same lease/result-write callbacks and revalidate ownership before writing replacement evidence or canonical segments.

The durable transcription lifecycle has the same rule at every commit boundary, not only at final success. Standalone transcription preflight reserves cleanup of incomplete attempts and then reacquires the reservation before creating the next `Transcription(status='pending')` row. OpenAI and local Whisper reserve the `running` transition and any `error` transition. OpenAI non-chunk source evidence is reserved before the raw JSON write; long-audio error manifests and the final done manifest are also published only while the attempt owns the result-write reservation. The final manifest reservation remains held through the `done` commit.

`tests/smoke_transcription_dispatch_fencing.py` covers callback propagation, fallback cleanup ordering, result-write lease loss, and stale-attempt invalidation. `tests/smoke_transcription_state_fencing.py` covers preflight cleanup/creation, provider running/error transitions, non-chunk evidence ordering, and chunk manifest ordering without external provider calls.
