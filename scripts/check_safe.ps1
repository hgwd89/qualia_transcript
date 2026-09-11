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

exit 0
