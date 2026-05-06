$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running integrated analysis no-ai dry-run check (no external API, no DB save)."
python tests/smoke_integrated_analysis_noai.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] integrated analysis no-ai dry-run check passed."
    exit 0
}

Write-Host "[FAIL] integrated analysis no-ai dry-run check failed (exit code: $exitCode)."
exit $exitCode
