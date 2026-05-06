$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running review queue smoke check (temporary DB, no external API)."
python tests/smoke_review_queue.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] review queue smoke check passed."
    exit 0
}

Write-Host "[FAIL] review queue smoke check failed (exit code: $exitCode)."
exit $exitCode
