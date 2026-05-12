$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running approved AIAnalysis output gate smoke check. No external API will be called."
python tests/smoke_output_analysis_gate.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] output analysis gate smoke check passed."
    exit 0
}

Write-Host "[FAIL] output analysis gate smoke check failed (exit code: $exitCode)."
exit $exitCode
