$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running integrated analysis preview UI check."
Write-Host "[INFO] No OpenAI API, Whisper, or DB save will be executed."

python tests/smoke_integrated_analysis_preview_ui.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] integrated analysis preview UI check failed"
    exit $LASTEXITCODE
}

Write-Host "[PASS] integrated analysis preview UI check passed."
exit 0
