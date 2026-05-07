$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running approved quote section smoke check. No external API will be called."
python tests/smoke_outputs_approved_quotes.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] approved quote section smoke check passed."
    exit 0
}

Write-Host "[FAIL] approved quote section smoke check failed (exit code: $exitCode)."
exit $exitCode

