$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Transcription smoke check will call OpenAI transcription API exactly once."
Write-Host "[INFO] Running transcription smoke check..."
python tests/smoke_transcription.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] transcription smoke check passed."
    exit 0
}

Write-Host "[FAIL] transcription smoke check failed (exit code: $exitCode)."
exit $exitCode
