$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running read-only production readiness audit..."
python scripts/audit_production_readiness_v2.py @args
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] production readiness audit passed."
    exit 0
}

if ($exitCode -eq 2) {
    Write-Host "[WARN] production readiness audit has warnings under --strict."
    exit 2
}

Write-Host "[FAIL] production readiness audit found blocking conditions (exit code: $exitCode)."
exit $exitCode
