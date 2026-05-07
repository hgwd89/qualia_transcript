$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running quote candidate UI smoke check (temporary DB, no external API)."
python tests/smoke_quote_candidates_ui.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] quote candidate UI smoke check passed."
    exit 0
}

Write-Host "[FAIL] quote candidate UI smoke check failed (exit code: $exitCode)."
exit $exitCode
