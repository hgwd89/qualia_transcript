$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running v0.2 model smoke check (temporary DB, no external API)."
python tests/smoke_v02_models.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] v0.2 model smoke check passed."
    exit 0
}

Write-Host "[FAIL] v0.2 model smoke check failed (exit code: $exitCode)."
exit $exitCode
