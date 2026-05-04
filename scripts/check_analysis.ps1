$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Analysis smoke check will call OpenAI API exactly once."
Write-Host "[INFO] Running analysis smoke check..."
python tests/smoke_analysis.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] analysis smoke check passed."
    exit 0
}

Write-Host "[FAIL] analysis smoke check failed (exit code: $exitCode)."
exit $exitCode
