$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

Write-Host "[INFO] Mapping smoke check will call OpenAI API exactly once."
Write-Host "[INFO] Running mapping smoke check..."
python tests/smoke_mapping.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] mapping smoke check passed."
    exit 0
}

Write-Host "[FAIL] mapping smoke check failed (exit code: $exitCode)."
exit $exitCode
