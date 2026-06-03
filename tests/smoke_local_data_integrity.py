import json
import os
import sqlite3
import sys
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote


REQUIRED_TABLES = {
    "projects",
    "participants",
    "interviews",
    "transcriptions",
    "segments",
    "utterance_mappings",
    "segment_flags",
    "speaker_assignments",
    "ai_analyses",
}


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return ok


def add_failure(failures: int, name: str, ok: bool, detail: str = "") -> int:
    return failures + (0 if print_result(name, ok, detail) else 1)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_db_path(root: Path) -> Path | None:
    explicit = os.getenv("QUALIA_LOCAL_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        import config
    except Exception:
        return None

    uri = getattr(config, "DATABASE_URI", "")
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        return None

    raw_path = unquote(uri[len(prefix):])
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate

    # Flask-SQLAlchemy stores relative sqlite paths under the instance folder.
    instance_candidate = root / "instance" / raw_path
    if instance_candidate.exists():
        return instance_candidate

    return root / raw_path


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def segment_fingerprint(conn: sqlite3.Connection, limit: int = 20) -> dict:
    rows = conn.execute(
        """
        SELECT id, interview_id, seq, speaker_label, speaker_role, text
        FROM segments
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    payload = [
        {
            "id": row["id"],
            "interview_id": row["interview_id"],
            "seq": row["seq"],
            "speaker_label": row["speaker_label"],
            "speaker_role": row["speaker_role"],
            "text": row["text"],
        }
        for row in rows
    ]
    digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "sample_size": len(payload),
        "sha256": digest,
        "segment_ids": [item["id"] for item in payload],
    }


def ai_analysis_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT analysis_type, COUNT(*) AS c
        FROM ai_analyses
        GROUP BY analysis_type
        ORDER BY analysis_type
        """
    ).fetchall()
    return {str(row["analysis_type"]): int(row["c"]) for row in rows}


def load_baseline(path: str) -> dict | None:
    if not path:
        return None
    baseline_path = Path(path).expanduser()
    if not baseline_path.is_file():
        raise FileNotFoundError(f"baseline not found: {baseline_path}")
    return json.loads(baseline_path.read_text(encoding="utf-8"))


def raw_transcript_snapshot_count(root: Path) -> int:
    raw_dir = root / "outputs" / "raw_transcripts"
    if not raw_dir.exists():
        return 0
    return len([p for p in raw_dir.glob("*.json") if p.is_file()])


def main() -> int:
    root = repo_root()
    failures = 0

    db_path = resolve_db_path(root)
    if not db_path or not db_path.exists():
        print("[SKIP] local DB not found. Set QUALIA_LOCAL_DB_PATH to run this manual check.")
        return 0

    failures = add_failure(failures, "local DB exists", db_path.is_file(), str(db_path))

    baseline = None
    try:
        baseline = load_baseline(os.getenv("QUALIA_LOCAL_DATA_BASELINE", "").strip())
        failures = add_failure(failures, "baseline loaded", True) if baseline else failures
    except Exception as e:
        failures = add_failure(failures, "baseline loaded", False, f"{type(e).__name__}: {e}")

    conn: sqlite3.Connection | None = None
    try:
        conn = connect_read_only(db_path)
        failures = add_failure(failures, "DB opened read-only", True)

        tables = table_names(conn)
        missing_tables = sorted(REQUIRED_TABLES - tables)
        failures = add_failure(
            failures,
            "required tables exist",
            not missing_tables,
            "" if not missing_tables else ", ".join(missing_tables),
        )
        if missing_tables:
            print(f"\nSummary: FAIL ({failures} checks failed)")
            return 1

        counts = {table: count_rows(conn, table) for table in sorted(REQUIRED_TABLES)}
        for table, count in counts.items():
            print(f"[INFO] {table} count={count}")

        if counts["segments"] == 0:
            print("[SKIP] local DB has no segments; no research data integrity sample to validate.")
            print("\nSummary: PASS")
            return 0

        failures = add_failure(failures, "segments present", True, f"count={counts['segments']}")
        failures = add_failure(
            failures,
            "segments have non-empty text",
            scalar(conn, "SELECT COUNT(*) FROM segments WHERE text IS NULL OR TRIM(text) = ''") == 0,
        )
        failures = add_failure(
            failures,
            "segments reference interviews",
            scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM segments s
                LEFT JOIN interviews i ON i.id = s.interview_id
                WHERE i.id IS NULL
                """,
            )
            == 0,
        )
        failures = add_failure(
            failures,
            "utterance mappings reference segments",
            scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM utterance_mappings um
                LEFT JOIN segments s ON s.id = um.segment_id
                WHERE s.id IS NULL
                """,
            )
            == 0,
        )
        failures = add_failure(
            failures,
            "segment flags reference segments",
            scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM segment_flags sf
                LEFT JOIN segments s ON s.id = sf.segment_id
                WHERE s.id IS NULL
                """,
            )
            == 0,
        )
        failures = add_failure(
            failures,
            "speaker assignments reference interviews",
            scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM speaker_assignments sa
                LEFT JOIN interviews i ON i.id = sa.interview_id
                WHERE i.id IS NULL
                """,
            )
            == 0,
        )

        fingerprint = segment_fingerprint(conn)
        analysis_counts = ai_analysis_counts(conn)
        raw_count = raw_transcript_snapshot_count(root)
        print(f"[INFO] segment sample fingerprint={fingerprint}")
        print(f"[INFO] ai_analysis_counts={analysis_counts}")
        print(f"[INFO] raw_transcript_snapshot_count={raw_count}")

        if baseline:
            failures = add_failure(
                failures,
                "segment sample fingerprint matches baseline",
                baseline.get("segment_fingerprint") == fingerprint,
            )
            failures = add_failure(
                failures,
                "AIAnalysis counts match baseline",
                baseline.get("ai_analysis_counts") == analysis_counts,
            )
            expected_raw_count = baseline.get("raw_transcript_snapshot_count")
            if expected_raw_count is not None:
                failures = add_failure(
                    failures,
                    "raw transcript snapshot count is not below baseline",
                    raw_count >= int(expected_raw_count),
                    f"current={raw_count} baseline={expected_raw_count}",
                )
        else:
            print("[INFO] baseline not configured; structural checks and fingerprints were reported only.")
    except Exception as e:
        failures = add_failure(failures, "local data integrity check", False, f"{type(e).__name__}: {e}")
    finally:
        if conn is not None:
            conn.close()

    if failures == 0:
        print("\nSummary: PASS")
        return 0

    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
