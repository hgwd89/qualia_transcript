$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running atomic job admission smoke check (temporary SQLite DB, no external API)..."
python tests/smoke_job_admission.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] atomic job admission smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] atomic job admission smoke checks passed."
exit 0
