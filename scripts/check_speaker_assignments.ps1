$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running speaker assignment check (external APIなし, reversible check)..."
python tests/smoke_speaker_assignments.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] speaker assignment smoke checks passed."
    exit 0
}

Write-Host "[FAIL] speaker assignment smoke checks failed (exit code: $exitCode)."
exit $exitCode
