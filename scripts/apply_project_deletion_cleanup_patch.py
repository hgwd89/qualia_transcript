from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


path = "services/project_deletion.py"

replace_once(
    path,
    '''def _remove_file(path: Path, errors: list[str]) -> int:\n    if not path.exists():\n        return 0\n    try:\n        path.unlink()\n        return 1\n    except OSError as exc:\n        errors.append(f"file cleanup failed: {path}: {exc}")\n        return 0\n\n\ndef cleanup_project_storage''',
    '''def _remove_file(path: Path, errors: list[str]) -> int:\n    if not path.exists():\n        return 0\n    try:\n        path.unlink()\n        return 1\n    except OSError as exc:\n        errors.append(f"file cleanup failed: {path}: {exc}")\n        return 0\n\n\ndef _remove_safe_id_dir(root: Path, value: int, label: str, errors: list[str]) -> int:\n    try:\n        path = _safe_id_dir(root, value)\n    except (OSError, ValueError) as exc:\n        errors.append(f"{label} cleanup path rejected: id={int(value)}: {exc}")\n        return 0\n    return _remove_dir(path, errors)\n\n\ndef cleanup_project_storage''',
)

replace_once(
    path,
    '''    removed += _remove_dir(_safe_id_dir(output_root, plan.project_id), errors)\n    for interview_id in plan.interview_ids:\n        removed += _remove_dir(_safe_id_dir(upload_root, interview_id), errors)\n''',
    '''    removed += _remove_safe_id_dir(\n        output_root, plan.project_id, "project output", errors\n    )\n    for interview_id in plan.interview_ids:\n        removed += _remove_safe_id_dir(\n            upload_root, interview_id, "interview upload", errors\n        )\n''',
)

replace_once(
    path,
    '''    raw_root = (output_root / "raw_transcripts").resolve()\n    try:\n        raw_root.relative_to(output_root)\n    except ValueError as exc:\n        raise ValueError("raw transcript root escapes OUTPUT_DIR") from exc\n    if raw_root.is_dir():\n        for transcription_id in plan.transcription_ids:\n            for path in raw_root.glob(f"transcription_{int(transcription_id)}_*.json"):\n                if path.is_file():\n                    removed += _remove_file(path, errors)\n\n    logs_root = (base_root / "logs").resolve()\n    try:\n        logs_root.relative_to(base_root)\n    except ValueError as exc:\n        raise ValueError("processing log root escapes BASE_DIR") from exc\n    for job_id in plan.processing_job_ids:\n        removed += _remove_file(logs_root / f"processing_job_{int(job_id)}.log", errors)\n''',
    '''    raw_root = (output_root / "raw_transcripts").resolve()\n    try:\n        raw_root.relative_to(output_root)\n    except ValueError:\n        errors.append("raw transcript cleanup path rejected: escapes OUTPUT_DIR")\n    else:\n        if raw_root.is_dir():\n            for transcription_id in plan.transcription_ids:\n                for path in raw_root.glob(f"transcription_{int(transcription_id)}_*.json"):\n                    if path.is_file() or path.is_symlink():\n                        removed += _remove_file(path, errors)\n\n    logs_root = (base_root / "logs").resolve()\n    try:\n        logs_root.relative_to(base_root)\n    except ValueError:\n        errors.append("processing log cleanup path rejected: escapes BASE_DIR")\n    else:\n        for job_id in plan.processing_job_ids:\n            removed += _remove_file(logs_root / f"processing_job_{int(job_id)}.log", errors)\n''',
)

print("project deletion cleanup patch applied")
