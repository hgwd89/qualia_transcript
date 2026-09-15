$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

# GitHub-hosted Windows runners may default Python stdout/stderr to a legacy
# code page. These smokes intentionally exercise Japanese API errors/statuses,
# so force UTF-8 to prevent the test harness itself from failing on output.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running durable processing job smoke check (temporary DB, no external API)..."
python tests/smoke_processing_jobs.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] durable processing job smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] durable processing job smoke checks passed."

Write-Host "[INFO] Running flow input fencing smoke check..."
python tests/smoke_flow_input_fencing.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] flow input fencing smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] flow input fencing smoke checks passed."

Write-Host "[INFO] Running mapping result scope smoke check..."
python tests/smoke_mapping_result_scope.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] mapping result scope smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] mapping result scope smoke checks passed."

Write-Host "[INFO] Running mapping source provenance smoke check..."
python tests/smoke_mapping_source_provenance.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] mapping source provenance smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] mapping source provenance smoke checks passed."

Write-Host "[INFO] Running mapping input currentness smoke check..."
python tests/smoke_mapping_input_currentness.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] mapping input currentness smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] mapping input currentness smoke checks passed."

Write-Host "[INFO] Running semantic source provenance smoke check..."
python tests/smoke_semantic_source_provenance.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] semantic source provenance smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] semantic source provenance smoke checks passed."

Write-Host "[INFO] Running moderator/interviewer role normalization smoke check..."
python tests/smoke_role_normalization.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] role normalization smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] role normalization smoke checks passed."

Write-Host "[INFO] Running durable interview job UI smoke check..."
python tests/smoke_interview_job_ui.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] durable interview job UI smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] durable interview job UI smoke checks passed."

Write-Host "[INFO] Running durable secondary analysis job smoke check..."
python tests/smoke_secondary_analysis_jobs.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] durable secondary analysis job smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] durable secondary analysis job smoke checks passed."

exit 0
