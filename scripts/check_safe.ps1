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

Write-Host "[INFO] Running generated-file integrity smoke check..."
python tests/smoke_generated_file_integrity.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] generated-file integrity smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] generated-file integrity smoke checks passed."

Write-Host "[INFO] Running media upload integrity smoke check..."
python tests/smoke_media_upload_integrity.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] media upload integrity smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] media upload integrity smoke checks passed."

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

exit 0
