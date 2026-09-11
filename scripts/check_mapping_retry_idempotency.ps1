$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "[INFO] Running mapping retry idempotency smoke check (temporary SQLite DB, no external API)..."
python tests/smoke_mapping_retry_idempotency.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] mapping retry idempotency smoke checks failed (exit code: $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-Host "[PASS] mapping retry idempotency smoke checks passed."
exit 0
