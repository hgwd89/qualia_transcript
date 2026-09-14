import hashlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def has_code(items, code: str, *, file_id: int | None = None) -> bool:
    for item in items:
        if item.get("code") != code:
            continue
        if file_id is None:
            return True
        context = item.get("context") or {}
        if int(context.get("generated_file_id") or 0) == int(file_id):
            return True
    return False


def build_valid_xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    stream = io.BytesIO()
    workbook = Workbook()
    try:
        workbook.active["A1"] = "snapshot-consistency"
        workbook.save(stream)
    finally:
        workbook.close()
    return stream.getvalue()


def build_generic_zip_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.txt", "hash-valid but not an OOXML workbook")
    return stream.getvalue()


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    for entry in (repo_root, scripts_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))

    import config

    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }

    with tempfile.TemporaryDirectory(prefix="qualia_readiness_artifact_snapshot_") as tmp:
        root = Path(tmp)
        db_path = root / "readiness.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.UPLOAD_DIR = str(root / "uploads")
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            import audit_production_readiness_v2 as readiness
            from models import db
            from models.generated_file import GeneratedFile
            from models.project import Project
            from services.file_manager import open_output_target_for_write, prepare_output_target
            from services.readiness_validation import validate_generated_artifact_stream

            valid_xlsx = build_valid_xlsx_bytes()
            replacement_zip = build_generic_zip_bytes()
            replacement_hash = hashlib.sha256(replacement_zip).hexdigest()

            failures += check(
                "race fixture starts as a structurally valid XLSX",
                validate_generated_artifact_stream(io.BytesIO(valid_xlsx), "xlsx") is None,
            )
            failures += check(
                "replacement fixture is hashable but structurally not XLSX",
                validate_generated_artifact_stream(io.BytesIO(replacement_zip), "xlsx") is not None,
            )

            app = create_app()
            with app.app_context():
                project = Project(name="Readiness artifact snapshot consistency")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                target = prepare_output_target(project_id, "snapshot-race.xlsx")
                opened = open_output_target_for_write(target)
                try:
                    opened.stream.write(valid_xlsx)
                finally:
                    opened.close()

                generated = GeneratedFile(
                    project_id=project_id,
                    file_type="analysis",
                    file_format="xlsx",
                    original_filename="snapshot-race.xlsx",
                    stored_path=target.stored_path,
                    generation_params_json=json.dumps(
                        {"artifact_sha256": replacement_hash},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                db.session.add(generated)
                db.session.commit()
                generated_id = int(generated.id)
                stored_path = str(generated.stored_path)
                db.session.remove()
                db.engine.dispose()

            output_path = output_dir / Path(stored_path)
            original_open = readiness.open_managed_file_for_read
            swapped = {"done": False}

            def swap_before_hash_open(root_value, requested_stored_path):
                if not swapped["done"] and str(requested_stored_path) == stored_path:
                    # Reproduce the old TOCTOU window: pathname structure could be
                    # validated from generation A, then the namespace could change
                    # to generation B immediately before the hash-bound managed open.
                    output_path.write_bytes(replacement_zip)
                    swapped["done"] = True
                return original_open(root_value, requested_stored_path)

            readiness.open_managed_file_for_read = swap_before_hash_open
            try:
                report = readiness.audit(db_path, output_dir, backup_dir)
            finally:
                readiness.open_managed_file_for_read = original_open

            failures += check(
                "race fixture swaps the pathname immediately before the hash-bound open",
                swapped["done"],
            )
            failures += check(
                "readiness rejects structure from the same hash-verified replacement snapshot",
                has_code(
                    report.get("blockers", []),
                    "generated_file_invalid",
                    file_id=generated_id,
                ),
                f"blockers={report.get('blockers', [])}",
            )
            failures += check(
                "replacement bytes satisfy the registered hash so rejection is structural, not hash mismatch",
                not has_code(
                    report.get("blockers", []),
                    "generated_file_byte_integrity_invalid",
                    file_id=generated_id,
                ),
                f"blockers={report.get('blockers', [])}",
            )
        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            if original_config["BACKUP_DIR"] is not None:
                config.BACKUP_DIR = original_config["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
