from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from pathlib import Path


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


def fake_provider(_system, _user, _schema, **_kwargs):
    return {
        "findings": [
            {
                "point": "テスト用の発見です",
                "evidence_quote": "テスト用の回答です",
                "participant_codes": ["P01"],
                "question_codes": [],
                "confidence": "high",
            }
        ],
        "implications": "テスト用の示唆です",
        "unresolved": "追加確認なし",
    }


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    tests_dir = repo_root / "tests"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))

    import smoke_analysis

    with tempfile.TemporaryDirectory(prefix="qualia_provider_isolation_source_") as tmp:
        root = Path(tmp)
        source_db = root / "canonical-sentinel.db"
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

        before_hash = sha256_file(source_db)
        rc = smoke_analysis.run_isolated_provider_smoke(
            source_db=source_db,
            provider_call=fake_provider,
        )
        after_hash = sha256_file(source_db)

        failures += check(
            "providerless isolated analysis smoke passes",
            rc == 0,
            f"exit={rc}",
        )
        failures += check(
            "canonical sentinel SQLite bytes are unchanged",
            before_hash == after_hash,
            f"before={before_hash} after={after_hash}",
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
            "canonical sentinel row is unchanged",
            marker is not None and marker[0] == "must-remain-unchanged",
            f"marker={marker!r}",
        )
        failures += check(
            "application schema was never initialized in canonical sentinel",
            "projects" not in tables
            and "interviews" not in tables
            and "ai_analyses" not in tables,
            f"tables={sorted(tables)!r}",
        )

    source_text = (tests_dir / "smoke_analysis.py").read_text(encoding="utf-8")
    failures += check(
        "provider smoke has no hard-coded live interview selector",
        "interview_id = 4" not in source_text
        and "Interview.query.get(4)" not in source_text,
    )
    failures += check(
        "canonical credential read is explicitly read-only",
        "?mode=ro" in source_text and "PRAGMA query_only=ON" in source_text,
    )
    failures += check(
        "provider smoke releases process runtime locks before temp cleanup",
        "release_process_runtime_locks" in source_text,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
