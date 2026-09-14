import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def load_audit(repo_root: Path):
    scripts_dir = repo_root / "scripts"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    path = scripts_dir / "audit_production_readiness_v2.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_v2_raw_integrity", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_fixture(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE processing_jobs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                interview_id INTEGER,
                question_id INTEGER,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                progress_json TEXT,
                request_json TEXT,
                created_at DATETIME,
                started_at DATETIME
            );
            CREATE TABLE interview_flow_questions (id INTEGER PRIMARY KEY);
            CREATE TABLE media_files (
                id INTEGER PRIMARY KEY,
                interview_id INTEGER,
                stored_path TEXT,
                file_size_bytes INTEGER,
                content_sha256 TEXT
            );
            CREATE TABLE transcriptions (
                id INTEGER PRIMARY KEY,
                media_file_id INTEGER,
                status TEXT,
                started_at DATETIME,
                completed_at DATETIME
            );
            CREATE TABLE generated_files (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                file_type TEXT,
                file_format TEXT,
                stored_path TEXT,
                generation_params_json TEXT
            );
            CREATE TABLE raw_snapshot_tombstones (
                snapshot_name TEXT PRIMARY KEY,
                snapshot_sha256 TEXT NOT NULL,
                owner_token TEXT NOT NULL DEFAULT 'fixture',
                project_id INTEGER NOT NULL DEFAULT 1,
                interview_id INTEGER NOT NULL DEFAULT 1,
                transcription_id INTEGER NOT NULL DEFAULT 1,
                deleted_at_utc TEXT NOT NULL DEFAULT '2026-09-14T00:00:00+00:00'
            );
            """
        )
        con.commit()
    finally:
        con.close()


def payload_bytes(*, transcription_id: int, text: str, include_hash: bool = True) -> bytes:
    payload = {
        "transcription_id": transcription_id,
        "interview_id": transcription_id,
        "created_at_utc": "2026-09-14T00:00:00+00:00",
        "text": text,
    }
    if include_hash:
        payload["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def raw_codes(report: dict, bucket: str) -> set[str]:
    return {
        str(item.get("code"))
        for item in report.get(bucket, [])
        if str(item.get("code") or "").startswith("raw_snapshot")
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    audit_mod = load_audit(repo_root)

    # Keep this regression focused on raw snapshot evidence integrity.
    audit_mod.base_audit = lambda *_args, **_kwargs: {"blockers": [], "warnings": [], "info": {}}
    audit_mod.validate_latest_backup = lambda _backup_dir: (None, None)

    failures = 0
    with tempfile.TemporaryDirectory(prefix="qualia_raw_readiness_integrity_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        output_dir = root / "outputs"
        upload_dir = root / "uploads"
        backup_dir = root / "backups"
        raw_dir = output_dir / "raw_transcripts"
        raw_dir.mkdir(parents=True)
        upload_dir.mkdir()
        backup_dir.mkdir()
        create_fixture(db_path)

        current_name = "current_hashed.json"
        legacy_name = "legacy_unproven.json"
        historical_name = "historical_tombstoned.json"
        current_bytes = payload_bytes(transcription_id=1, text="current source", include_hash=True)
        legacy_bytes = payload_bytes(transcription_id=2, text="legacy source", include_hash=False)
        historical_bytes = payload_bytes(
            transcription_id=3,
            text="retained historical source",
            include_hash=False,
        )
        (raw_dir / current_name).write_bytes(current_bytes)
        (raw_dir / legacy_name).write_bytes(legacy_bytes)
        (raw_dir / historical_name).write_bytes(historical_bytes)

        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT INTO raw_snapshot_tombstones(snapshot_name, snapshot_sha256) VALUES (?, ?)",
                (historical_name, hashlib.sha256(historical_bytes).hexdigest()),
            )
            con.commit()
        finally:
            con.close()

        report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        info = report.get("info") or {}
        failures += check(
            "current hashed raw snapshot remains verified",
            "raw_snapshot_invalid" not in raw_codes(report, "blockers")
            and int(info.get("raw_text_snapshot_count") or 0) == 3,
            f"blockers={report.get('blockers', [])} info={info}",
        )
        failures += check(
            "legacy raw snapshot without self hash is explicit warning",
            "raw_snapshot_byte_integrity_unproven" in raw_codes(report, "warnings")
            and int(info.get("raw_snapshot_unproven_hash_count") or 0) == 1,
            f"warnings={report.get('warnings', [])} info={info}",
        )
        failures += check(
            "tombstoned historical raw source is proven by database full-file hash",
            "raw_snapshot_tombstone_integrity_invalid" not in raw_codes(report, "blockers")
            and int(info.get("raw_snapshot_tombstone_integrity_issue_count") or 0) == 0
            and int(info.get("raw_snapshot_tombstone_count") or 0) == 1,
            f"blockers={report.get('blockers', [])} warnings={report.get('warnings', [])} info={info}",
        )

        # Change the retained historical JSON and make its internal self-hash valid.
        # The immutable tombstone hash must still detect that the whole file changed.
        tampered_bytes = payload_bytes(
            transcription_id=3,
            text="tampered historical source",
            include_hash=True,
        )
        (raw_dir / historical_name).write_bytes(tampered_bytes)
        tampered_report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "tombstone full-file hash blocks self-consistent historical tampering",
            "raw_snapshot_tombstone_integrity_invalid"
            in raw_codes(tampered_report, "blockers"),
            str(tampered_report.get("blockers", [])),
        )

        (raw_dir / historical_name).write_bytes(historical_bytes)
        restored_report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "exact historical bytes restore tombstone integrity",
            "raw_snapshot_tombstone_integrity_invalid"
            not in raw_codes(restored_report, "blockers"),
            str(restored_report.get("blockers", [])),
        )

        (raw_dir / historical_name).unlink()
        missing_report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "missing tombstoned historical raw source is a blocker",
            "raw_snapshot_tombstone_integrity_invalid"
            in raw_codes(missing_report, "blockers"),
            str(missing_report.get("blockers", [])),
        )

        # A malformed current self-hash remains a hard integrity failure rather
        # than being downgraded to the legacy-unproven warning.
        bad_payload = json.loads(current_bytes.decode("utf-8"))
        bad_payload["sha256"] = "0" * 64
        (raw_dir / current_name).write_text(
            json.dumps(bad_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        bad_hash_report = audit_mod.audit(db_path, output_dir, backup_dir, upload_dir)
        failures += check(
            "present but mismatched raw self hash remains a blocker",
            "raw_snapshot_invalid" in raw_codes(bad_hash_report, "blockers"),
            str(bad_hash_report.get("blockers", [])),
        )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
