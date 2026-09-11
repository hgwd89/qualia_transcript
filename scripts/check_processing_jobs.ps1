$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running durable processing job smoke check (temporary DB, no external API)..."
python tests/smoke_processing_jobs.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] durable processing job smoke checks passed."
    exit 0
}

Write-Host "[FAIL] durable processing job smoke checks failed (exit code: $exitCode)."
exit $exitCode
