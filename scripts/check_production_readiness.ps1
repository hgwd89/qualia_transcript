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
    Write-Host "[INFO] Running read-only project-scoped production readiness audit..."
    python scripts/audit_production_readiness_project.py @args
} else {
    Write-Host "[INFO] Running read-only database-wide production readiness audit..."
    python scripts/audit_production_readiness_v2.py @args
}
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[PASS] production readiness audit passed."
    exit 0
}

if ($exitCode -eq 2) {
    Write-Host "[WARN] production readiness audit has warnings under --strict."
    exit 2
}

Write-Host "[FAIL] production readiness audit found blocking conditions (exit code: $exitCode)."
exit $exitCode
