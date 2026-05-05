$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running segment flag check (external APIなし, reversible check)..."
python tests/smoke_flags.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] segment flag smoke checks passed."
    exit 0
}

Write-Host "[FAIL] segment flag smoke checks failed (exit code: $exitCode)."
exit $exitCode
