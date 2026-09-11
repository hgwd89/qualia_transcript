$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running professional output/data-integrity smoke check (no external API)..."
python tests/smoke_professional_integrity.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] professional integrity smoke checks passed."
    exit 0
}

Write-Host "[FAIL] professional integrity smoke checks failed (exit code: $exitCode)."
exit $exitCode
