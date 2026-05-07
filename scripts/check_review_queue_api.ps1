$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running review queue API smoke check (temporary DB, no external API)."
python tests/smoke_review_queue_api.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] review queue API smoke check passed."
    exit 0
}

Write-Host "[FAIL] review queue API smoke check failed (exit code: $exitCode)."
exit $exitCode
