from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedFileOwnershipReport:
    blockers: list[dict]
    warnings: list[dict]
    checked_count: int


def normalized_stored_path_parts(stored_path: str) -> tuple[str, ...] | None:
    """Return lexical path parts after collapsing separators/dot segments.

    This is intentionally filesystem-independent so Windows-style separators in
    historical rows are interpreted consistently on every readiness platform.
    Leading parent traversal is rejected as unscoped here; the main readiness
    path-safety gate separately reports escape/out-of-root paths.
    """
    parts: list[str] = []
    for raw_part in str(stored_path or "").replace("\\", "/").split("/"):
        part = raw_part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return tuple(parts)


def stored_path_project_id(stored_path: str) -> int | None:
    """Return the positive numeric project namespace encoded by stored_path."""
    parts = normalized_stored_path_parts(stored_path)
    if not parts or len(parts) < 2:
        return None
    try:
        project_id = int(parts[0])
    except (TypeError, ValueError):
        return None
    return project_id if project_id > 0 else None


def inspect_generated_file_ownership(
    con: sqlite3.Connection,
    *,
    project_id: int | None = None,
) -> GeneratedFileOwnershipReport:
    """Find cross-project GeneratedFile ownership inconsistencies read-only.

    `main.` is used deliberately so project-scoped readiness can still compare a
    selected project's generated-file rows with the canonical interview table
    rather than TEMP VIEWs that would hide a cross-project target.
    """
    params: tuple[object, ...] = ()
    where = ""
    if project_id is not None:
        project_id = int(project_id)
        where = "WHERE gf.project_id=?"
        params = (project_id,)

    rows = con.execute(
        f"""
        SELECT
            gf.id,
            gf.project_id,
            gf.interview_id,
            gf.stored_path,
            i.id AS linked_interview_id,
            i.project_id AS interview_project_id
        FROM main.generated_files gf
        LEFT JOIN main.interviews i ON i.id=gf.interview_id
        {where}
        ORDER BY gf.id
        """,
        params,
    ).fetchall()

    blockers: list[dict] = []
    warnings: list[dict] = []
    for row in rows:
        generated_file_id = int(row["id"])
        raw_project_id = row["project_id"]
        if raw_project_id is None:
            blockers.append({
                "code": "generated_file_project_missing",
                "message": "GeneratedFile has no owning project",
                "context": {"generated_file_id": generated_file_id},
            })
            continue

        owner_project_id = int(raw_project_id)
        interview_id = row["interview_id"]
        if interview_id is not None:
            if row["linked_interview_id"] is None:
                blockers.append({
                    "code": "generated_file_interview_missing",
                    "message": "GeneratedFile references a missing interview",
                    "context": {
                        "generated_file_id": generated_file_id,
                        "project_id": owner_project_id,
                        "interview_id": int(interview_id),
                    },
                })
            elif int(row["interview_project_id"]) != owner_project_id:
                blockers.append({
                    "code": "generated_file_interview_project_mismatch",
                    "message": "GeneratedFile interview belongs to another project",
                    "context": {
                        "generated_file_id": generated_file_id,
                        "project_id": owner_project_id,
                        "interview_id": int(interview_id),
                        "interview_project_id": int(row["interview_project_id"]),
                    },
                })

        stored_path = str(row["stored_path"] or "")
        path_project_id = stored_path_project_id(stored_path)
        if path_project_id is None:
            warnings.append({
                "code": "generated_file_project_path_unscoped",
                "message": "GeneratedFile stored_path does not encode a project directory",
                "context": {
                    "generated_file_id": generated_file_id,
                    "project_id": owner_project_id,
                    "stored_path": stored_path,
                },
            })
        elif path_project_id != owner_project_id:
            blockers.append({
                "code": "generated_file_project_path_mismatch",
                "message": "GeneratedFile stored_path belongs to another project namespace",
                "context": {
                    "generated_file_id": generated_file_id,
                    "project_id": owner_project_id,
                    "path_project_id": path_project_id,
                    "stored_path": stored_path,
                },
            })

    return GeneratedFileOwnershipReport(
        blockers=blockers,
        warnings=warnings,
        checked_count=len(rows),
    )
