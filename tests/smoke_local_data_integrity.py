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
    "interview_flow_questions",
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
        SELECT id, transcription_id, interview_id, seq, speaker_label, speaker_role, text
        FROM segments
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    payload = [
        {
            "id": row["id"],
            "transcription_id": row["transcription_id"],
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
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("baseline must be a JSON object")
    return data


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def raw_transcript_snapshot_manifest(root: Path) -> dict[str, str]:
    raw_dir = root / "outputs" / "raw_transcripts"
    if not raw_dir.exists():
        return {}
    return {
        p.name: file_sha256(p)
        for p in sorted(raw_dir.glob("*.json"), key=lambda x: x.name)
        if p.is_file()
    }


def expected_minimum_table_counts(baseline: dict | None) -> dict[str, int]:
    if not baseline:
        return {}
    source = baseline.get("minimum_table_counts")
    if source is None:
        source = baseline.get("table_counts", {})
    if not isinstance(source, dict):
        raise ValueError("minimum_table_counts/table_counts must be an object")
    return {str(k): int(v) for k, v in source.items()}


def expected_minimum_ai_counts(baseline: dict | None) -> dict[str, int]:
    if not baseline:
        return {}
    source = baseline.get("minimum_ai_analysis_counts")
    if source is None:
        source = baseline.get("ai_analysis_counts", {})
    if not isinstance(source, dict):
        raise ValueError("minimum_ai_analysis_counts/ai_analysis_counts must be an object")
    return {str(k): int(v) for k, v in source.items()}


def _run_check(root: Path) -> int:
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
            "segments reference transcriptions when transcription_id is set",
            scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM segments s
                LEFT JOIN transcriptions t ON t.id = s.transcription_id
                WHERE s.transcription_id IS NOT NULL AND t.id IS NULL
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
            "utterance mappings reference questions when question_id is set",
            scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM utterance_mappings um
                LEFT JOIN interview_flow_questions q ON q.id = um.question_id
                WHERE um.question_id IS NOT NULL AND q.id IS NULL
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
        failures = add_failure(
            failures,
            "speaker assignments reference participants when participant_id is set",
            scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM speaker_assignments sa
                LEFT JOIN participants p ON p.id = sa.participant_id
                WHERE sa.participant_id IS NOT NULL AND p.id IS NULL
                """,
            )
            == 0,
        )

        fingerprint = segment_fingerprint(conn)
        analysis_counts = ai_analysis_counts(conn)
        raw_manifest = raw_transcript_snapshot_manifest(root)
        raw_manifest_fingerprint = sha256(
            json.dumps(raw_manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        print(f"[INFO] segment sample fingerprint={fingerprint}")
        print(f"[INFO] ai_analysis_counts={analysis_counts}")
        print(f"[INFO] raw_transcript_snapshot_count={len(raw_manifest)}")
        print(f"[INFO] raw_transcript_manifest_sha256={raw_manifest_fingerprint}")

        if counts["segments"] == 0 and not baseline:
            print("[SKIP] local DB has no segments; structural checks passed but no research-data sample was available.")

        if baseline:
            minimum_counts = expected_minimum_table_counts(baseline)
            for table, expected in sorted(minimum_counts.items()):
                if table not in counts:
                    failures = add_failure(
                        failures,
                        f"minimum table count: {table}",
                        False,
                        "table not included in integrity count set",
                    )
                    continue
                current = counts[table]
                failures = add_failure(
                    failures,
                    f"minimum table count: {table}",
                    current >= expected,
                    f"current={current} baseline_min={expected}",
                )

            expected_fingerprint = baseline.get("segment_fingerprint")
            if expected_fingerprint is not None:
                failures = add_failure(
                    failures,
                    "segment sample fingerprint matches baseline",
                    expected_fingerprint == fingerprint,
                )

            minimum_ai_counts = expected_minimum_ai_counts(baseline)
            for analysis_type, expected in sorted(minimum_ai_counts.items()):
                current = analysis_counts.get(analysis_type, 0)
                failures = add_failure(
                    failures,
                    f"minimum AIAnalysis count: {analysis_type}",
                    current >= expected,
                    f"current={current} baseline_min={expected}",
                )

            expected_raw_manifest = baseline.get("raw_transcript_snapshots")
            if expected_raw_manifest is not None:
                if not isinstance(expected_raw_manifest, dict):
                    raise ValueError("raw_transcript_snapshots must be an object of filename -> sha256")
                for filename, expected_hash in sorted(expected_raw_manifest.items()):
                    current_hash = raw_manifest.get(str(filename))
                    failures = add_failure(
                        failures,
                        f"raw transcript snapshot preserved: {filename}",
                        current_hash == str(expected_hash),
                        "missing" if current_hash is None else "hash mismatch" if current_hash != str(expected_hash) else "",
                    )

            expected_raw_count = baseline.get("raw_transcript_snapshot_count")
            if expected_raw_count is not None:
                failures = add_failure(
                    failures,
                    "raw transcript snapshot count is not below baseline",
                    len(raw_manifest) >= int(expected_raw_count),
                    f"current={len(raw_manifest)} baseline={expected_raw_count}",
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


def main() -> int:
    root = repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from services.runtime_lock import RuntimeLockError, runtime_lock

    try:
        with runtime_lock("reader"):
            return _run_check(root)
    except RuntimeLockError as exc:
        print(
            "[FAIL] local data integrity check refused while backup/restore "
            f"maintenance is active: {exc}"
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
