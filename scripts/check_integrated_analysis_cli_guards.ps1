$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running integrated analysis CLI guard check (no OpenAI API, no Whisper, no DB save)."
python tests/smoke_integrated_analysis_cli_guards.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] integrated analysis CLI guard check passed."
    exit 0
}

Write-Host "[FAIL] integrated analysis CLI guard check failed (exit code: $exitCode)."
exit $exitCode