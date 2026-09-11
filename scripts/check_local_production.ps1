$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running local production/backup smoke check (no external API)..."
python tests/smoke_local_production.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] local production/backup smoke checks passed."
    exit 0
}

Write-Host "[FAIL] local production/backup smoke checks failed (exit code: $exitCode)."
exit $exitCode
