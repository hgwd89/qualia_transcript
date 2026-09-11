$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running AI analysis review migration smoke check (no external API)..."
python tests/smoke_analysis_review_migration.py
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host "[FAIL] AI analysis review migration smoke check failed (exit code: $exitCode)."
    exit $exitCode
}

Write-Host "[INFO] Running AI analysis review/export smoke check (no external API)..."
python tests/smoke_analysis_review.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] AI analysis review/export smoke checks passed."
    exit 0
}

Write-Host "[FAIL] AI analysis review/export smoke checks failed (exit code: $exitCode)."
exit $exitCode
