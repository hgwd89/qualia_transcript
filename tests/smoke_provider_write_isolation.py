from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_test_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 16000)


def fake_mapping_provider(_system, user, _schema, **_kwargs):
    segment_match = re.search(r"segment_id:(\d+)", user)
    q2_match = re.search(r"id:(\d+) \[Q2\]", user)
    if not segment_match or not q2_match:
        raise AssertionError(f"expected fixture IDs were not present in provider prompt: {user}")
    return {
        "mappings": [
            {
                "segment_id": int(segment_match.group(1)),
                "question_id": int(q2_match.group(1)),
                "confidence": 0.99,
                "is_unclassified": False,
            }
        ]
    }


def fake_transcription_client_factory():
    def create(*_args, **_kwargs):
        return SimpleNamespace(text="テスト用の文字起こしです。")

    return SimpleNamespace(
        audio=SimpleNamespace(
            transcriptions=SimpleNamespace(create=create),
        )
    )


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    tests_dir = repo_root / "tests"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))

    import smoke_mapping
    import smoke_transcription

    with tempfile.TemporaryDirectory(prefix="qualia_provider_write_isolation_") as tmp:
        root = Path(tmp)
        source_db = root / "canonical-sentinel.db"
        source_audio = root / "provider-source.wav"
        write_test_wav(source_audio)

        con = sqlite3.connect(source_db)
        try:
            con.executescript(
                """
                CREATE TABLE app_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT,
                    updated_at DATETIME
                );
                CREATE TABLE canonical_marker (
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO canonical_marker(id, value)
                VALUES (1, 'must-remain-unchanged');
                """
            )
            con.commit()
        finally:
            con.close()

        db_before = sha256_file(source_db)
        audio_before = sha256_file(source_audio)

        mapping_rc = smoke_mapping.run_isolated_mapping_smoke(
            source_db=source_db,
            provider_call=fake_mapping_provider,
        )
        failures += check(
            "providerless isolated mapping smoke passes",
            mapping_rc == 0,
            f"exit={mapping_rc}",
        )
        failures += check(
            "canonical sentinel bytes unchanged after mapping smoke",
            sha256_file(source_db) == db_before,
            f"before={db_before} after={sha256_file(source_db)}",
        )

        transcription_rc = smoke_transcription.run_isolated_transcription_smoke(
            source_db=source_db,
            source_audio=source_audio,
            client_factory=fake_transcription_client_factory,
        )
        failures += check(
            "providerless isolated transcription smoke passes",
            transcription_rc == 0,
            f"exit={transcription_rc}",
        )
        failures += check(
            "canonical sentinel bytes unchanged after transcription smoke",
            sha256_file(source_db) == db_before,
            f"before={db_before} after={sha256_file(source_db)}",
        )
        failures += check(
            "provider source audio bytes remain unchanged",
            sha256_file(source_audio) == audio_before,
            f"before={audio_before} after={sha256_file(source_audio)}",
        )

        readonly = sqlite3.connect(
            f"file:{source_db.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            readonly.execute("PRAGMA query_only=ON")
            marker = readonly.execute(
                "SELECT value FROM canonical_marker WHERE id=1"
            ).fetchone()
            tables = {
                str(row[0])
                for row in readonly.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            readonly.close()

        failures += check(
            "canonical sentinel row remains unchanged",
            marker is not None and marker[0] == "must-remain-unchanged",
            f"marker={marker!r}",
        )
        forbidden_tables = {
            "projects",
            "interviews",
            "media_files",
            "transcriptions",
            "segments",
            "utterance_mappings",
        }
        failures += check(
            "application schema is never initialized in canonical sentinel",
            not (tables & forbidden_tables),
            f"tables={sorted(tables)!r}",
        )

    mapping_source = (tests_dir / "smoke_mapping.py").read_text(encoding="utf-8")
    transcription_source = (tests_dir / "smoke_transcription.py").read_text(encoding="utf-8")
    failures += check(
        "mapping smoke has no hard-coded canonical research selector",
        "interview_id = 4" not in mapping_source
        and "segment_id = 40" not in mapping_source
        and "Interview.query.get(4)" not in mapping_source,
    )
    failures += check(
        "transcription smoke has no hard-coded canonical research selector",
        "Project.query.get(2)" not in transcription_source
        and "Interview.query.get(4)" not in transcription_source
        and "Transcription.query.get(8)" not in transcription_source
        and "Segment.query.get(40)" not in transcription_source,
    )
    failures += check(
        "provider smoke credential reads are explicitly read-only",
        all(
            "?mode=ro" in source and "PRAGMA query_only=ON" in source
            for source in (mapping_source, transcription_source)
        ),
    )
    failures += check(
        "provider smokes release process runtime locks before temp cleanup",
        all(
            "release_process_runtime_locks" in source
            for source in (mapping_source, transcription_source)
        ),
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
