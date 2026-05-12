$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running AIAnalysis approval smoke check. No external API will be called."
python tests/smoke_analysis_approval.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] analysis approval smoke check passed."
    exit 0
}

Write-Host "[FAIL] analysis approval smoke check failed (exit code: $exitCode)."
exit $exitCode
