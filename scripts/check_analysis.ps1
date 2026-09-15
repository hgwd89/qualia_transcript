$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Analysis provider smoke will call OpenAI API exactly once."
Write-Host "[INFO] Canonical research data is read only for the stored provider credential; all analysis writes use a disposable temporary database."
Write-Host "[INFO] Running isolated analysis provider smoke check..."
python tests/smoke_analysis.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] isolated analysis provider smoke check passed."
    exit 0
}

Write-Host "[FAIL] isolated analysis provider smoke check failed (exit code: $exitCode)."
exit $exitCode
