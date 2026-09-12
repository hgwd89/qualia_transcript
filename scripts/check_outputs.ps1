$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running Word/Excel output smoke check. No external API will be called."
python tests/smoke_outputs.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] output smoke check failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] output smoke check passed."

Write-Host "[INFO] Running self-contained formatted-sheet speaker-assignment regression..."
python tests/smoke_formatted_speaker_assignment.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] formatted-sheet speaker-assignment regression failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] formatted-sheet speaker-assignment regression passed."

exit 0
