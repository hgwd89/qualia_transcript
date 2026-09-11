$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running stale processing job recovery smoke check..."
python tests/smoke_job_recovery.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] stale processing job recovery smoke checks passed."
    exit 0
}

Write-Host "[FAIL] stale processing job recovery smoke checks failed (exit code: $exitCode)."
exit $exitCode
