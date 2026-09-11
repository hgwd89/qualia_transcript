$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running processing job fencing smoke check (temporary SQLite DB, no external API)..."
python tests/smoke_job_fencing.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] processing job fencing smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] processing job fencing smoke checks passed."
exit 0
