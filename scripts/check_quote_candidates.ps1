$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running quote candidate smoke check (temporary DB, no external API)."
python tests/smoke_quote_candidates.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] quote candidate smoke check passed."
    exit 0
}

Write-Host "[FAIL] quote candidate smoke check failed (exit code: $exitCode)."
exit $exitCode
