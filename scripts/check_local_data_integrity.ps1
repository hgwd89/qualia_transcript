$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Running manual local data integrity check."
Write-Host "[INFO] This check opens the local SQLite DB in read-only mode."
Write-Host "[INFO] It does not call OpenAI, run Whisper, or generate Word/Excel outputs."
Write-Host "[INFO] Set QUALIA_LOCAL_DB_PATH to override the DB path."
Write-Host "[INFO] Set QUALIA_LOCAL_DATA_BASELINE to compare representative fingerprints."

python tests/smoke_local_data_integrity.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] local data integrity check failed."
    exit $LASTEXITCODE
}

Write-Host "[PASS] local data integrity check passed."
exit 0
