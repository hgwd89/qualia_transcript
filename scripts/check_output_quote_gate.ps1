$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running approved quote gate smoke check. No external API will be called."
python tests/smoke_output_quote_gate.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] output quote gate smoke check passed."
    exit 0
}

Write-Host "[FAIL] output quote gate smoke check failed (exit code: $exitCode)."
exit $exitCode

