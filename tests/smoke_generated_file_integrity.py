import hashlib
import json
import sys
import tempfile
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

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_generated_file_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'generated.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")

        try:
            from sqlalchemy import event

            from app import create_app
            from models import db
            from models.generated_file import GeneratedFile
            from models.project import Project
            from services.file_manager import (
                file_exists,
                get_full_path,
                open_output_target_for_write,
                prepare_output_target,
                register_generated_file,
                safe_output_filename,
            )

            def write_managed_target(target, data: bytes) -> None:
                opened = open_output_target_for_write(target)
                try:
                    opened.stream.write(data)
                finally:
                    opened.close()

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Path / unsafe : project ?")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                target = prepare_output_target(
                    project_id,
                    "分析/../レポート:危険?.xlsx",
                )
                target_path = Path(target.full_path)
                output_root = Path(config.OUTPUT_DIR).resolve()
                project_dir = (output_root / str(project_id)).resolve()
                failures += check(
                    "output target remains project-scoped with internal unique name",
                    target_path.parent == project_dir
                    and target_path.name == Path(target.stored_path).name
                    and target_path.name != target.filename
                    and target_path.suffix == Path(target.filename).suffix
                    and target.stored_path.startswith(f"{project_id}/"),
                    f"target={target}",
                )
                failures += check(
                    "generated download filename is cross-platform safe",
                    all(ch not in target.filename for ch in '<>:"/\\|?*')
                    and "\\" not in target.stored_path,
                    target.filename,
                )

                long_download_name = ("長" * 110) + ".xlsx"
                long_target = prepare_output_target(project_id, long_download_name)
                long_storage_name = Path(long_target.stored_path).name
                failures += check(
                    "long download name keeps bounded UUID-only storage basename",
                    long_target.filename == long_download_name
                    and long_storage_name.endswith(".xlsx")
                    and len(long_storage_name.encode("utf-8")) < 64,
                    f"storage_bytes={len(long_storage_name.encode('utf-8'))}",
                )

                write_managed_target(target, b"generated")
                gf = register_generated_file(
                    target,
                    project_id=project_id,
                    file_type="analysis",
                    file_format="xlsx",
                )
                params = json.loads(gf.generation_params_json or "{}")
                failures += check(
                    "successful registration keeps exact managed write and DB row aligned",
                    target.written_stat is not None
                    and gf.id is not None
                    and gf.original_filename == target.filename
                    and gf.stored_path == target.stored_path
                    and file_exists(gf)
                    and Path(get_full_path(gf)) == target_path
                    and target_path.read_bytes() == b"generated",
                    f"file_id={gf.id} stored_path={gf.stored_path}",
                )
                failures += check(
                    "ordinary generated-file registration records exact SHA-256",
                    params.get("artifact_sha256") == hashlib.sha256(b"generated").hexdigest(),
                    f"params={params}",
                )

                first_collision = prepare_output_target(project_id, "同時生成.xlsx")
                second_collision = prepare_output_target(project_id, "同時生成.xlsx")
                first_collision_path = Path(first_collision.full_path)
                second_collision_path = Path(second_collision.full_path)
                failures += check(
                    "same download filename receives distinct managed paths",
                    first_collision.filename == second_collision.filename == "同時生成.xlsx"
                    and first_collision.stored_path != second_collision.stored_path
                    and first_collision_path != second_collision_path,
                    f"first={first_collision.stored_path} second={second_collision.stored_path}",
                )

                write_managed_target(first_collision, b"first-success")
                first_collision_gf = register_generated_file(
                    first_collision,
                    project_id=project_id,
                    file_type="analysis",
                    file_format="xlsx",
                )
                before_count = GeneratedFile.query.count()

                write_managed_target(second_collision, b"second-fails")
                session = db.session()

                def fail_before_commit(_session):
                    raise RuntimeError("simulated generated-file DB commit failure")

                event.listen(session, "before_commit", fail_before_commit, once=True)
                raised = False
                try:
                    register_generated_file(
                        second_collision,
                        project_id=project_id,
                        file_type="analysis",
                        file_format="xlsx",
                    )
                except RuntimeError as exc:
                    raised = "simulated generated-file DB commit failure" in str(exc)
                finally:
                    if event.contains(session, "before_commit", fail_before_commit):
                        event.remove(session, "before_commit", fail_before_commit)

                failures += check(
                    "failed colliding registration removes only its own managed generation",
                    raised
                    and first_collision_path.is_file()
                    and first_collision_path.read_bytes() == b"first-success"
                    and file_exists(first_collision_gf)
                    and not second_collision_path.exists()
                    and GeneratedFile.query.count() == before_count,
                    (
                        f"raised={raised} first_exists={first_collision_path.exists()} "
                        f"second_exists={second_collision_path.exists()} "
                        f"count={GeneratedFile.query.count()}"
                    ),
                )

                outside = root / "outside.txt"
                outside.write_text("must survive", encoding="utf-8")
                malicious = GeneratedFile(
                    project_id=project_id,
                    file_type="analysis",
                    file_format="csv",
                    original_filename="outside.txt",
                    stored_path="../outside.txt",
                )
                db.session.add(malicious)
                db.session.commit()
                invalid_rejected = False
                try:
                    get_full_path(malicious)
                except ValueError:
                    invalid_rejected = True
                failures += check(
                    "malicious stored_path cannot escape OUTPUT_DIR",
                    invalid_rejected and not file_exists(malicious) and outside.is_file(),
                )

                legacy = GeneratedFile(
                    project_id=project_id,
                    file_type="analysis",
                    file_format="xlsx",
                    original_filename=None,
                    stored_path=gf.stored_path,
                )
                db.session.add(legacy)
                db.session.commit()

                client = app.test_client()
                normal_response = client.get(f"/api/outputs/{gf.id}/download")
                normal_status = normal_response.status_code
                normal_data = normal_response.data
                normal_headers = dict(normal_response.headers)
                normal_response.close()
                failures += check(
                    "download route streams registered managed file bytes",
                    normal_status == 200 and normal_data == b"generated",
                    f"status={normal_status} bytes={len(normal_data)}",
                )
                failures += check(
                    "download route preserves length modification and etag metadata",
                    normal_headers.get("Content-Length") == "9"
                    and bool(normal_headers.get("Last-Modified"))
                    and bool(normal_headers.get("ETag")),
                    (
                        f"length={normal_headers.get('Content-Length')} "
                        f"last_modified={normal_headers.get('Last-Modified')} "
                        f"etag={normal_headers.get('ETag')}"
                    ),
                )

                range_response = client.get(
                    f"/api/outputs/{gf.id}/download",
                    headers={"Range": "bytes=2-4"},
                )
                range_status = range_response.status_code
                range_data = range_response.data
                range_headers = dict(range_response.headers)
                range_response.close()
                failures += check(
                    "download route preserves byte-range behavior through verified snapshot",
                    range_status == 206
                    and range_data == b"ner"
                    and range_headers.get("Content-Range") == "bytes 2-4/9"
                    and range_headers.get("Content-Length") == "3"
                    and range_headers.get("Accept-Ranges") == "bytes",
                    (
                        f"status={range_status} data={range_data!r} "
                        f"range={range_headers.get('Content-Range')}"
                    ),
                )

                target_path.write_bytes(b"tampered!")
                tampered_response = client.get(f"/api/outputs/{gf.id}/download")
                tampered_status = tampered_response.status_code
                tampered_response.close()
                failures += check(
                    "hash-bound ordinary download rejects later in-place byte tampering",
                    tampered_status == 409,
                    f"status={tampered_status}",
                )

                target_path.write_bytes(b"generated")
                restored_response = client.get(f"/api/outputs/{gf.id}/download")
                restored_status = restored_response.status_code
                restored_data = restored_response.data
                restored_response.close()
                failures += check(
                    "restoring exact registered bytes restores ordinary download",
                    restored_status == 200 and restored_data == b"generated",
                    f"status={restored_status} data={restored_data!r}",
                )

                legacy_response = client.get(f"/api/outputs/{legacy.id}/download")
                legacy_status = legacy_response.status_code
                legacy_data = legacy_response.data
                legacy_disposition = legacy_response.headers.get("Content-Disposition", "")
                legacy_response.close()
                failures += check(
                    "download route supports legacy rows without registered byte hash",
                    legacy_status == 200
                    and legacy_data == b"generated"
                    and Path(gf.stored_path).name in legacy_disposition,
                    f"status={legacy_status} disposition={legacy_disposition}",
                )

                response = client.get(f"/api/outputs/{malicious.id}/download")
                failures += check(
                    "download route rejects escaped stored_path",
                    response.status_code == 404 and outside.is_file(),
                    f"status={response.status_code}",
                )
                response.close()

                invalid_name_raised = False
                try:
                    safe_output_filename("...")
                except ValueError:
                    invalid_name_raised = True
                failures += check(
                    "empty-after-sanitize filename is rejected",
                    invalid_name_raised,
                )
                failures += check(
                    "Windows reserved filename is neutralized",
                    safe_output_filename("CON.txt") == "_CON.txt",
                )

                db.session.remove()
                db.engine.dispose()

            wired = {
                "services/report_verbatim.py": "file_type=\"verbatim\"",
                "services/report_formatted.py": "file_type=\"formatted_sheet\"",
                "services/report_analysis.py": "file_type=\"analysis\"",
                "services/report_approved_analysis.py": "file_type=\"approved_analysis\"",
            }
            for relative, marker in wired.items():
                text = (repo_root / relative).read_text(encoding="utf-8")
                failures += check(
                    f"{relative} uses managed generated-file registration",
                    "prepare_output_target" in text
                    and "open_output_target_for_write" in text
                    and "register_generated_file" in text
                    and marker in text,
                )

        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
