$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running safe smoke checks..."
python tests/smoke_safe.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] safe smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] safe smoke checks passed."

Write-Host "[INFO] Running launcher runtime-config smoke check..."
powershell -ExecutionPolicy Bypass -File scripts/check_runtime_config.ps1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] launcher runtime-config smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] launcher runtime-config smoke checks passed."

Write-Host "[INFO] Running generated-file integrity smoke check..."
python tests/smoke_generated_file_integrity.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] generated-file integrity smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] generated-file integrity smoke checks passed."

Write-Host "[INFO] Running generated-output write fencing smoke check..."
python tests/smoke_generated_output_write_fencing.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] generated-output write fencing smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] generated-output write fencing smoke checks passed."

Write-Host "[INFO] Running media upload integrity smoke check..."
python tests/smoke_media_upload_integrity.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] media upload integrity smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] media upload integrity smoke checks passed."

Write-Host "[INFO] Running linked managed-storage guard smoke check..."
python tests/smoke_linked_storage_guards.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] linked managed-storage guard smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] linked managed-storage guard smoke checks passed."

Write-Host "[INFO] Running backup/restore integrity hardening smoke check..."
python tests/smoke_backup_integrity_hardening.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] backup/restore integrity hardening smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] backup/restore integrity hardening smoke checks passed."

Write-Host "[INFO] Running backup snapshot fencing smoke check..."
python tests/smoke_backup_snapshot_fencing.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] backup snapshot fencing smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] backup snapshot fencing smoke checks passed."

Write-Host "[INFO] Running managed backup collector guard smoke check..."
python tests/smoke_backup_collect_tree_guard.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] managed backup collector guard smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] managed backup collector guard smoke checks passed."

Write-Host "[INFO] Running backup atomic-publish smoke check..."
python tests/smoke_backup_atomic_publish.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] backup atomic-publish smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] backup atomic-publish smoke checks passed."

Write-Host "[INFO] Running backup service-boundary smoke check..."
python tests/smoke_backup_service_boundary.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] backup service-boundary smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] backup service-boundary smoke checks passed."

Write-Host "[INFO] Running runtime maintenance exclusion smoke check..."
python tests/smoke_runtime_maintenance_lock.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] runtime maintenance exclusion smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] runtime maintenance exclusion smoke checks passed."

Write-Host "[INFO] Running app-factory runtime exclusion smoke check..."
python tests/smoke_app_factory_runtime_lock.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] app-factory runtime exclusion smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] app-factory runtime exclusion smoke checks passed."

Write-Host "[INFO] Running admin CLI maintenance exclusion smoke check..."
python tests/smoke_admin_cli_maintenance_lock.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] admin CLI maintenance exclusion smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] admin CLI maintenance exclusion smoke checks passed."

Write-Host "[INFO] Running read-only runtime exclusion smoke check..."
python tests/smoke_runtime_reader_lock.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] read-only runtime exclusion smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] read-only runtime exclusion smoke checks passed."

Write-Host "[INFO] Running durable job admission smoke check..."
python tests/smoke_job_admission.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] durable job admission smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] durable job admission smoke checks passed."

Write-Host "[INFO] Running project deletion lifecycle smoke check..."
python tests/smoke_project_deletion_lifecycle.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] project deletion lifecycle smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] project deletion lifecycle smoke checks passed."

Write-Host "[INFO] Running project scope/delete guard smoke check..."
python tests/smoke_project_scope_delete_guards.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] project scope/delete guard smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] project scope/delete guard smoke checks passed."

Write-Host "[INFO] Running interview creation scope smoke check..."
python tests/smoke_interview_create_scope.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] interview creation scope smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] interview creation scope smoke checks passed."

Write-Host "[INFO] Running role normalization/update integrity smoke check..."
python tests/smoke_role_normalization.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] role normalization/update integrity smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] role normalization/update integrity smoke checks passed."

Write-Host "[INFO] Running SQLite foreign key smoke check..."
python tests/smoke_sqlite_foreign_keys.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] SQLite foreign key smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] SQLite foreign key smoke checks passed."

Write-Host "[INFO] Running readiness FK orphan smoke check..."
python tests/smoke_readiness_foreign_keys.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] readiness FK orphan smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] readiness FK orphan smoke checks passed."

Write-Host "[INFO] Running readiness traceability smoke check..."
python tests/smoke_readiness_traceability.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] readiness traceability smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] readiness traceability smoke checks passed."

Write-Host "[INFO] Running project-scoped readiness smoke check..."
python tests/smoke_readiness_project_scope.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] project-scoped readiness smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] project-scoped readiness smoke checks passed."

Write-Host "[INFO] Running resumed analysis UI smoke check..."
python tests/smoke_analysis_resume_ui.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] resumed analysis UI smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] resumed analysis UI smoke checks passed."

Write-Host "[INFO] Running Windows DPAPI secret-store smoke check..."
python tests/smoke_secret_store.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] DPAPI secret-store smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] DPAPI secret-store smoke checks passed."

exit 0