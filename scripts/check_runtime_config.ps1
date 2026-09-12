$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir "runtime_config.ps1")

$failures = 0

function Assert-Equal {
    param(
        [string]$Name,
        [string]$Actual,
        [string]$Expected
    )
    if ($Actual -eq $Expected) {
        Write-Host "[PASS] $Name"
    } else {
        Write-Host "[FAIL] $Name`: expected=$Expected actual=$Actual"
        $script:failures += 1
    }
}

Assert-Equal -Name "IPv4 unspecified maps to loopback" `
    -Actual (Get-QualiaAppUrl -HostAddress "0.0.0.0" -Port 5000) `
    -Expected "http://127.0.0.1:5000/"

Assert-Equal -Name "IPv6 loopback is bracketed" `
    -Actual (Get-QualiaAppUrl -HostAddress "::1" -Port 5000) `
    -Expected "http://[::1]:5000/"

Assert-Equal -Name "IPv6 unspecified maps to reachable loopback" `
    -Actual (Get-QualiaAppUrl -HostAddress "::" -Port 5000) `
    -Expected "http://[::1]:5000/"

Assert-Equal -Name "expanded IPv6 unspecified maps to reachable loopback" `
    -Actual (Get-QualiaAppUrl -HostAddress "0:0:0:0:0:0:0:0" -Port 5001) `
    -Expected "http://[::1]:5001/"

Assert-Equal -Name "already bracketed IPv6 is normalized once" `
    -Actual (Get-QualiaAppUrl -HostAddress "[::1]" -Port 5002) `
    -Expected "http://[::1]:5002/"

Assert-Equal -Name "hostname remains unbracketed" `
    -Actual (Get-QualiaAppUrl -HostAddress "localhost" -Port 5003) `
    -Expected "http://localhost:5003/"

if ($failures -gt 0) {
    Write-Host "`nSummary: FAIL ($failures checks failed)"
    exit 1
}

Write-Host "`nSummary: PASS"
exit 0
