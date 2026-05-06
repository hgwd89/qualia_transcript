$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running output flag smoke check. No external API will be called."
python tests/smoke_outputs_flags.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] output flag smoke check passed."
    exit 0
}

Write-Host "[FAIL] output flag smoke check failed (exit code: $exitCode)."
exit $exitCode
