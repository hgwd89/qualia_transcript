import sys
import tempfile
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


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    for entry in (repo_root, scripts_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }

    with tempfile.TemporaryDirectory(prefix="qualia_generated_readiness_") as tmp:
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
            from audit_production_readiness_v2 import audit
            from models import db
            from models.generated_file import GeneratedFile
            from models.project import Project
            from services.file_manager import (
                open_output_target_for_write,
                prepare_output_target,
                register_generated_file,
            )

            app = create_app()
            with app.app_context():
                project = Project(name="Generated readiness integrity")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                registered_bytes = b"col1,col2\r\n1,2\r\n"
                target = prepare_output_target(project_id, "analysis.csv")
                opened = open_output_target_for_write(target)
                try:
                    opened.stream.write(registered_bytes)
                finally:
                    opened.close()
                generated = register_generated_file(
                    target,
                    project_id=project_id,
                    file_type="analysis",
                    file_format="csv",
                )
                generated_id = int(generated.id)

                legacy = GeneratedFile(
                    project_id=project_id,
                    file_type="analysis",
                    file_format="csv",
                    original_filename="legacy-analysis.csv",
                    stored_path=generated.stored_path,
                    generation_params_json=None,
                )
                db.session.add(legacy)
                db.session.commit()
                legacy_id = int(legacy.id)
                stored_path = str(generated.stored_path)
                db.session.remove()
                db.engine.dispose()

            output_path = output_dir / Path(stored_path)
            initial = audit(db_path, output_dir, backup_dir)
            initial_info = initial.get("info", {}).get("generated_file_byte_integrity", {})
            failures += check(
                "readiness verifies hash-bound ordinary output",
                initial_info.get("verified") == 1
                and not has_code(
                    initial.get("blockers", []),
                    "generated_file_byte_integrity_invalid",
                    file_id=generated_id,
                ),
                f"info={initial_info} blockers={initial.get('blockers', [])}",
            )
            failures += check(
                "legacy ordinary output without hash is warning-only",
                initial_info.get("unproven") == 1
                and has_code(
                    initial.get("warnings", []),
                    "generated_file_byte_integrity_unproven",
                    file_id=legacy_id,
                )
                and not has_code(
                    initial.get("blockers", []),
                    "generated_file_byte_integrity_invalid",
                    file_id=legacy_id,
                ),
                f"info={initial_info} warnings={initial.get('warnings', [])}",
            )

            tampered_bytes = b"col1,col2\r\n9,2\r\n"
            failures += check(
                "tamper fixture preserves byte length and CSV structure",
                len(tampered_bytes) == len(registered_bytes),
            )
            output_path.write_bytes(tampered_bytes)
            tampered = audit(db_path, output_dir, backup_dir)
            tampered_info = tampered.get("info", {}).get("generated_file_byte_integrity", {})
            failures += check(
                "readiness blocks same-length in-place tampering of ordinary output",
                tampered_info.get("invalid") == 1
                and has_code(
                    tampered.get("blockers", []),
                    "generated_file_byte_integrity_invalid",
                    file_id=generated_id,
                ),
                f"info={tampered_info} blockers={tampered.get('blockers', [])}",
            )

            output_path.write_bytes(registered_bytes)
            restored = audit(db_path, output_dir, backup_dir)
            restored_info = restored.get("info", {}).get("generated_file_byte_integrity", {})
            failures += check(
                "restoring exact registered bytes restores readiness integrity",
                restored_info.get("verified") == 1
                and restored_info.get("invalid") == 0
                and not has_code(
                    restored.get("blockers", []),
                    "generated_file_byte_integrity_invalid",
                    file_id=generated_id,
                ),
                f"info={restored_info} blockers={restored.get('blockers', [])}",
            )
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            if original["BACKUP_DIR"] is not None:
                config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
