# Production transcription dispatch fencing

`services.processing_jobs._perform_transcription()` uses `services.transcription_dispatch.run_transcription()` as the durable production entry point.

That dispatcher must preserve the durable `lease_check` and `result_write_guard` callbacks on every provider path, including direct local Whisper and OpenAI-to-local fallback.

When OpenAI long-audio processing has already committed partial `Segment` rows and then falls back to local Whisper, the dispatcher must acquire the stronger result-write reservation before deleting those partial canonical rows. The local provider must then receive the same lease/result-write callbacks and revalidate ownership before writing replacement evidence or canonical segments.

`tests/smoke_transcription_dispatch_fencing.py` covers callback propagation, cleanup ordering, result-write lease loss, and stale-attempt invalidation without external provider calls.
