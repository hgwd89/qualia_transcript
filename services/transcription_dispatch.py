from __future__ import annotations

from collections.abc import Callable

from services.processing_result_guard import (
    discard_transcription_segments,
    invalidate_transcription_attempt,
)
from services.transcription import (
    get_fallback_provider,
    get_transcription_provider,
    run_local_whisper_transcription,
    run_openai_transcription,
)


LeaseCheck = Callable[[], object]


def _check_or_invalidate(transcription_id: int, lease_check: LeaseCheck | None) -> None:
    if lease_check is None:
        return
    try:
        lease_check()
    except Exception:
        invalidate_transcription_attempt(transcription_id)
        raise


def run_transcription(transcription_id: int, *, lease_check: LeaseCheck | None = None) -> dict:
    """Run transcription with fallback cleanup and optional durable-job fencing.

    OpenAI long-audio mode commits successful chunks incrementally. Before local
    fallback, those partial rows must be discarded or the local result would be
    appended to the same transcription. When a durable worker loses its lease,
    any rows committed by that stale transcription attempt are invalidated before
    control returns to the worker layer.
    """
    _check_or_invalidate(transcription_id, lease_check)
    provider = get_transcription_provider()

    if provider == "openai":
        try:
            result = run_openai_transcription(transcription_id)
        except Exception:
            _check_or_invalidate(transcription_id, lease_check)
            if get_fallback_provider() != "local_whisper":
                raise

            discarded = discard_transcription_segments(transcription_id)
            _check_or_invalidate(transcription_id, lease_check)
            result = run_local_whisper_transcription(transcription_id)
            _check_or_invalidate(transcription_id, lease_check)
            return {
                **(result or {}),
                "fallback_from": "openai",
                "fallback_discarded_partial_segment_count": discarded,
            }

        _check_or_invalidate(transcription_id, lease_check)
        return result

    result = run_local_whisper_transcription(transcription_id)
    _check_or_invalidate(transcription_id, lease_check)
    return result
