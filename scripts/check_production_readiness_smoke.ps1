$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running production readiness audit smoke check (temporary fixtures, no external API)..."
python tests/smoke_production_readiness.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] production readiness audit smoke checks passed."
    exit 0
}

Write-Host "[FAIL] production readiness audit smoke checks failed (exit code: $exitCode)."
exit $exitCode
