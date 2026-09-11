$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running project pipeline resilience smoke check..."
python tests/smoke_project_pipeline_resilience.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] project pipeline resilience smoke checks passed."
    exit 0
}

Write-Host "[FAIL] project pipeline resilience smoke checks failed (exit code: $exitCode)."
exit $exitCode
