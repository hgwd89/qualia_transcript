$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running safe smoke checks..."
python tests/smoke_safe.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] safe smoke checks passed."
    exit 0
}

Write-Host "[FAIL] safe smoke checks failed (exit code: $exitCode)."
exit $exitCode
