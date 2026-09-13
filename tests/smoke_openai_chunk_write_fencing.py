from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


class FakeSegmentQuery:
    def filter_by(self, **_kwargs):
        return self

    def count(self):
        return 0


class FakeSegment:
    query = FakeSegmentQuery()

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeTranscriptionQuery:
    def __init__(self, transcription):
        self.transcription = transcription

    def get(self, _transcription_id):
        return self.transcription


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.pending = []

    def add(self, value):
        self.events.append("add")
        self.pending.append(value)

    def commit(self):
        self.events.append("commit")
        self.pending.clear()

    def rollback(self):
        self.events.append("rollback")
        self.pending.clear()


class FakeSnapshot:
    full_path = "managed-media-snapshot.wav"

    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("close-media")


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import services.transcription as transcription

    original_names = [
        "db",
        "Segment",
        "Transcription",
        "create_media_read_snapshot",
        "_openai_client",
        "_probe_media_duration_sec",
        "_LONG_AUDIO_THRESHOLD_SEC",
        "_OPENAI_CHUNK_DURATION_SEC",
        "_OPENAI_CHUNK_OVERLAP_SEC",
        "_export_audio_chunk_wav",
        "_call_openai_transcription",
        "_extract_response_text",
        "_write_raw_transcript_snapshot",
        "_write_chunk_manifest",
        "_is_diarize_model",
    ]
    originals = {name: getattr(transcription, name) for name in original_names}

    events: list[str] = []
    interview = SimpleNamespace(id=77, status="pending")
    media = SimpleNamespace(interview=interview, duration_sec=61.0)
    tr = SimpleNamespace(
        media_file=media,
        whisper_model="whisper-1",
        language="ja",
        status="pending",
        started_at=None,
        completed_at=None,
        error_message=None,
        word_count=0,
    )
    session = FakeSession(events)

    class FakeDb:
        pass

    fake_db = FakeDb()
    fake_db.session = session

    class FakeTranscription:
        query = FakeTranscriptionQuery(tr)

    provider_calls = 0
    guard_calls = 0

    def fake_provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        events.append(f"provider-{provider_calls}")
        return {"text": f"chunk {provider_calls}."}

    def fake_guard():
        nonlocal guard_calls
        guard_calls += 1
        events.append(f"guard-{guard_calls}")
        if session.pending:
            raise AssertionError("result-write guard ran after pending Segment rows existed")
        return object()

    def fake_lease():
        events.append("lease")
        return object()

    def fake_snapshot(*, snapshot_tag=None, **_kwargs):
        events.append(f"snapshot-{snapshot_tag}")
        return f"raw/{snapshot_tag}.json", f"sha-{snapshot_tag}"

    def fake_manifest(**kwargs):
        events.append(f"manifest-{kwargs.get('status')}")
        return "raw/manifest.json"

    try:
        transcription.db = fake_db
        transcription.Segment = FakeSegment
        transcription.Transcription = FakeTranscription
        transcription.create_media_read_snapshot = lambda _media: FakeSnapshot(events)
        transcription._openai_client = lambda: object()
        transcription._probe_media_duration_sec = lambda _path: 61.0
        transcription._LONG_AUDIO_THRESHOLD_SEC = 60.0
        transcription._OPENAI_CHUNK_DURATION_SEC = 40.0
        transcription._OPENAI_CHUNK_OVERLAP_SEC = 0.0
        transcription._export_audio_chunk_wav = (
            lambda *, duration_sec, **_kwargs: float(duration_sec)
        )
        transcription._call_openai_transcription = fake_provider
        transcription._extract_response_text = lambda response: response["text"]
        transcription._write_raw_transcript_snapshot = fake_snapshot
        transcription._write_chunk_manifest = fake_manifest
        transcription._is_diarize_model = lambda _model_name: False

        result = transcription.run_openai_transcription(
            123,
            lease_check=fake_lease,
            result_write_guard=fake_guard,
        )

        first_guard = events.index("guard-1")
        first_snapshot = events.index("snapshot-chunk_01")
        first_add = events.index("add", first_snapshot)
        first_commit = events.index("commit", first_add)
        second_guard = events.index("guard-2")
        second_snapshot = events.index("snapshot-chunk_02")
        second_add = events.index("add", second_snapshot)
        second_commit = events.index("commit", second_add)
        final_guard = events.index("guard-3")
        final_commit = events.index("commit", final_guard)

        failures += check(
            "each long-form OpenAI chunk acquires result-write reservation before evidence and Segment writes",
            guard_calls == 3
            and provider_calls == 2
            and first_guard < first_snapshot < first_add < first_commit
            and second_guard < second_snapshot < second_add < second_commit
            and final_guard < final_commit
            and result.get("chunk_count") == 2
            and result.get("segment_count") == 2
            and tr.status == "done"
            and interview.status == "transcribed",
            f"events={events!r} result={result!r}",
        )

        failures += check(
            "durable chunk commits do not run a lease-only check after pending Segment rows exist",
            all(
                not (events[index] == "lease" and index > 0 and events[index - 1] == "add")
                for index in range(len(events))
            ),
            f"events={events!r}",
        )

    finally:
        for name, value in originals.items():
            setattr(transcription, name, value)

    source = (repo_root / "services" / "transcription.py").read_text(encoding="utf-8")
    failures += check(
        "chunk source acquires result-write guard before raw snapshot publication",
        "if result_write_guard is not None:\n                        result_write_guard()\n                    elif lease_check is not None:\n                        lease_check()\n\n                    raw_snapshot_path" in source
        and "if result_write_guard is None and lease_check is not None:\n                        lease_check()\n                    db.session.commit()" in source,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
