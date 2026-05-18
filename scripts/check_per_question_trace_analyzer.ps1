$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running per-question analyzer trace smoke check (temporary DB, no external API)."
python tests/smoke_per_question_trace_analyzer.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] per-question analyzer trace smoke check passed."
    exit 0
}

Write-Host "[FAIL] per-question analyzer trace smoke check failed (exit code: $exitCode)."
exit $exitCode
