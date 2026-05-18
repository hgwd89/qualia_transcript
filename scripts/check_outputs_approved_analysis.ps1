$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running approved analysis output smoke check. No external API will be called."
python tests/smoke_outputs_approved_analysis.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] approved analysis output smoke check passed."
    exit 0
}

Write-Host "[FAIL] approved analysis output smoke check failed (exit code: $exitCode)."
exit $exitCode
