$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running formatted-sheet speaker-assignment smoke check..."
python tests/smoke_formatted_speaker_assignment.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] formatted-sheet speaker-assignment smoke check passed."
    exit 0
}

Write-Host "[FAIL] formatted-sheet speaker-assignment smoke check failed (exit code: $exitCode)."
exit $exitCode
