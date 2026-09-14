$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

$projectScoped = $false
foreach ($arg in $args) {
    if ($arg -eq "--project-id" -or $arg -like "--project-id=*") {
        $projectScoped = $true
        break
    }
}

if ($projectScoped) {
    Write-Host "[INFO] Running final read-only project-scoped production readiness audit..."
} else {
    Write-Host "[INFO] Running final read-only database-wide production readiness audit..."
}
python scripts/run_with_runtime_reader.py scripts/audit_production_readiness_final.py @args
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] production readiness audit passed."
    exit 0
}

if ($exitCode -eq 2) {
    Write-Host "[WARN] production readiness audit has warnings under --strict."
    exit 2
}

if ($exitCode -eq 3) {
    Write-Host "[FAIL] production readiness audit refused while backup/restore maintenance is active."
    exit 3
}

Write-Host "[FAIL] production readiness audit found blocking conditions (exit code: $exitCode)."
exit $exitCode
