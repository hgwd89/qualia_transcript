$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

# GitHub-hosted Windows runners may default Python stdout/stderr to a legacy
# code page. The smoke intentionally exercises Japanese API errors/statuses,
# so force UTF-8 to prevent the test harness itself from failing on output.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running durable processing job smoke check (temporary DB, no external API)..."
python tests/smoke_processing_jobs.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] durable processing job smoke checks passed."
    exit 0
}

Write-Host "[FAIL] durable processing job smoke checks failed (exit code: $exitCode)."
exit $exitCode
