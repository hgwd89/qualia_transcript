# GeneratedFile project ownership contract

`GeneratedFile` is a derived deliverable record, but its ownership metadata is part of the research-delivery boundary. A file must never be registered as belonging to one project while its interview or managed output namespace belongs to another project.

## Write boundary

All runtime `GeneratedFile(...)` construction remains centralized in `services/file_manager.py`.

`register_generated_file()` requires:

- a clean SQLAlchemy session so its internal commit cannot publish unrelated caller mutations;
- an existing `Project` matching the supplied `project_id`;
- a project-scoped `stored_path` whose first managed path component is the same project ID;
- when `interview_id` is present, an existing `Interview` whose `project_id` matches the generated file project;
- the exact managed file generation produced by the output writer, including its registered SHA-256 and post-commit namespace verification.

On SQLite, ordinary registration starts `BEGIN IMMEDIATE` before ownership validation and holds that write reservation through the `GeneratedFile` commit. This prevents another connection from changing ownership-relevant database state between validation and registration. Non-SQLite ownership rows are selected with row locks where supported.

Formal approved-analysis export already owns a serialized source snapshot. It calls the registrar with `existing_write_reservation=True`; the registrar must preserve that transaction rather than rollback/restart it. This keeps formal source-provenance validation, workbook bytes, ownership validation, and file registration inside the same serialized database boundary.

If registration fails before commit, cleanup removes only the exact generated file generation that was produced for the rejected registration. Cross-project or dirty-session rejection must not leave an orphan deliverable on disk.

## Existing-data readiness

`services/generated_file_ownership.py` performs read-only checks for historical rows:

- `GeneratedFile.interview_id` must resolve to an interview in the same project;
- a numeric project directory encoded by `stored_path` must match `GeneratedFile.project_id`;
- path normalization treats `/` and `\\` consistently and collapses dot segments so lexical traversal cannot disguise another project's namespace;
- missing interview ownership and cross-project namespace mismatches are blockers;
- legacy root-level/unscoped paths are warnings rather than silently being treated as project-scoped proof.

Project-scoped readiness filters ownership checks by the selected `GeneratedFile.project_id` but resolves linked interviews from canonical `main.interviews`, so a cross-project target cannot be hidden by TEMP VIEW scoping.

## Final readiness composition

`scripts/audit_production_readiness_final.py` is the release-readiness entry point used by `scripts/check_production_readiness.ps1` for both database-wide and `--project-id` audits. The underlying v2/project audit remains the hardened core, but ownership is injected into its `_run_base_audit_on_snapshot(...)` boundary and therefore runs on the exact same read-only SQLite transaction as the rest of readiness.

No second ownership connection is opened after the v2 audit. The existing v2 `PRAGMA data_version` start/end check therefore covers ownership inspection as part of the same acceptance window. If another connection commits while ownership is being inspected, final readiness returns the existing `database_changed_during_audit` blocker and must be rerun on a quiescent dataset.

Direct invocation of the lower-level v2/project modules is an internal diagnostic path. The canonical professional-delivery gate is `scripts/check_production_readiness.ps1`, which routes through `audit_production_readiness_final.py`.

## Regression gates

The required Windows and Ubuntu Safe Smoke matrix runs:

- `tests/smoke_generated_file_project_ownership.py` — valid ownership, cross-project interview rejection, project-directory mismatch rejection, exact-generation cleanup, existing-row blockers, project-scoped visibility, and legacy unscoped warnings;
- `tests/smoke_generated_file_ownership_serialization.py` — a competing SQLite writer cannot change interview ownership while registration holds its write reservation;
- `tests/smoke_final_readiness_database_change_detection.py` — ownership inspection is proven to run inside the v2 SQLite read transaction, a concurrent WAL commit is detected by the existing v2 `database_changed_during_audit` gate, and a stable rerun does not invent a change blocker.

These tests are providerless and use temporary databases and managed directories only.
