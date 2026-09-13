from __future__ import annotations

import sys
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import services.transcription_dispatch as dispatch

    originals = {
        "get_transcription_provider": dispatch.get_transcription_provider,
        "get_fallback_provider": dispatch.get_fallback_provider,
        "run_openai_transcription": dispatch.run_openai_transcription,
        "run_local_whisper_transcription": dispatch.run_local_whisper_transcription,
        "discard_transcription_segments": dispatch.discard_transcription_segments,
        "invalidate_transcription_attempt": dispatch.invalidate_transcription_attempt,
    }

    try:
        events: list[str] = []

        def lease_check():
            events.append("lease")
            return object()

        def result_write_guard():
            events.append("guard")
            return object()

        def fake_local(transcription_id, *, lease_check=None, result_write_guard=None):
            events.append("local")
            assert transcription_id == 101
            assert lease_check is lease_cb
            assert result_write_guard is guard_cb
            return {"provider": "local"}

        lease_cb = lease_check
        guard_cb = result_write_guard
        dispatch.get_transcription_provider = lambda: "local_whisper"
        dispatch.run_local_whisper_transcription = fake_local

        direct = dispatch.run_transcription(
            101,
            lease_check=lease_cb,
            result_write_guard=guard_cb,
        )
        failures += check(
            "production direct-local dispatch preserves lease and result-write callbacks",
            direct == {"provider": "local"}
            and events == ["lease", "local", "lease"],
            f"events={events!r} result={direct!r}",
        )

        events.clear()

        def fake_openai(transcription_id, *, lease_check=None, result_write_guard=None):
            events.append("openai")
            assert transcription_id == 202
            assert lease_check is lease_cb
            assert result_write_guard is guard_cb
            raise RuntimeError("simulated OpenAI chunk failure")

        def fake_discard(transcription_id):
            events.append("cleanup")
            assert transcription_id == 202
            return 3

        def fake_fallback_local(transcription_id, *, lease_check=None, result_write_guard=None):
            events.append("local")
            assert transcription_id == 202
            assert lease_check is lease_cb
            assert result_write_guard is guard_cb
            return {"provider": "local"}

        dispatch.get_transcription_provider = lambda: "openai"
        dispatch.get_fallback_provider = lambda: "local_whisper"
        dispatch.run_openai_transcription = fake_openai
        dispatch.discard_transcription_segments = fake_discard
        dispatch.run_local_whisper_transcription = fake_fallback_local

        fallback = dispatch.run_transcription(
            202,
            lease_check=lease_cb,
            result_write_guard=guard_cb,
        )
        failures += check(
            "production fallback reserves canonical write before partial-segment cleanup",
            events == [
                "lease",
                "openai",
                "lease",
                "guard",
                "cleanup",
                "lease",
                "local",
                "lease",
            ]
            and fallback.get("fallback_from") == "openai"
            and fallback.get("fallback_discarded_partial_segment_count") == 3,
            f"events={events!r} result={fallback!r}",
        )

        events.clear()
        cleanup_started = False
        local_started = False

        def lost_guard():
            events.append("guard")
            raise RuntimeError("simulated result-write lease loss")

        def should_not_cleanup(_transcription_id):
            nonlocal cleanup_started
            cleanup_started = True
            raise AssertionError("cleanup started after result-write guard loss")

        def should_not_start_local(*_args, **_kwargs):
            nonlocal local_started
            local_started = True
            raise AssertionError("local fallback started after result-write guard loss")

        dispatch.discard_transcription_segments = should_not_cleanup
        dispatch.run_local_whisper_transcription = should_not_start_local
        guard_lost = False
        try:
            dispatch.run_transcription(
                202,
                lease_check=lease_cb,
                result_write_guard=lost_guard,
            )
        except RuntimeError as exc:
            guard_lost = "simulated result-write lease loss" in str(exc)

        failures += check(
            "result-write lease loss blocks cleanup and local fallback",
            guard_lost and not cleanup_started and not local_started,
            (
                f"guard_lost={guard_lost} cleanup_started={cleanup_started} "
                f"local_started={local_started} events={events!r}"
            ),
        )

        invalidated: list[int] = []
        provider_started = False

        def lost_lease():
            raise RuntimeError("simulated durable lease loss")

        def fake_invalidate(transcription_id):
            invalidated.append(int(transcription_id))

        def should_not_start_provider(*_args, **_kwargs):
            nonlocal provider_started
            provider_started = True
            raise AssertionError("provider started after durable lease loss")

        dispatch.invalidate_transcription_attempt = fake_invalidate
        dispatch.get_transcription_provider = lambda: "local_whisper"
        dispatch.run_local_whisper_transcription = should_not_start_provider
        lease_lost = False
        try:
            dispatch.run_transcription(303, lease_check=lost_lease)
        except RuntimeError as exc:
            lease_lost = "simulated durable lease loss" in str(exc)

        failures += check(
            "production dispatch invalidates stale attempt before provider startup",
            lease_lost and invalidated == [303] and not provider_started,
            (
                f"lease_lost={lease_lost} invalidated={invalidated!r} "
                f"provider_started={provider_started}"
            ),
        )

    finally:
        for name, value in originals.items():
            setattr(dispatch, name, value)

    source = (repo_root / "services" / "transcription_dispatch.py").read_text(encoding="utf-8")
    failures += check(
        "dispatcher source passes lease callback into every local Whisper path",
        source.count("lease_check=lease_check") >= 3
        and "result_write_guard()\n            discarded = discard_transcription_segments" in source,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
