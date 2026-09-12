$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $projectRoot "scripts\runtime_config.ps1")

$pythonExe = Get-QualiaPythonExecutable -ProjectDir $projectRoot
$oldHost = $env:APP_HOST
$oldPort = $env:APP_PORT
$failures = 0

function Check-RuntimeConfig {
    param(
        [string]$Name,
        [string]$HostValue,
        [int]$PortValue,
        [string]$ExpectedBrowserHost,
        [string]$ExpectedUrl
    )

    $env:APP_HOST = $HostValue
    $env:APP_PORT = "$PortValue"
    $runtime = Get-QualiaRuntimeConfig -ProjectDir $projectRoot -PythonExe $pythonExe
    $ok = (
        $runtime.Host -eq $HostValue -and
        $runtime.Port -eq $PortValue -and
        $runtime.BrowserHost -eq $ExpectedBrowserHost -and
        $runtime.Url -eq $ExpectedUrl
    )
    if ($ok) {
        Write-Host "[PASS] $Name"
    } else {
        Write-Host "[FAIL] $Name host=$($runtime.Host) browser=$($runtime.BrowserHost) url=$($runtime.Url)"
        $script:failures += 1
    }
}

try {
    Check-RuntimeConfig "IPv4 loopback URL" "127.0.0.1" 5121 "127.0.0.1" "http://127.0.0.1:5121/"
    Check-RuntimeConfig "IPv4 wildcard maps to loopback" "0.0.0.0" 5122 "127.0.0.1" "http://127.0.0.1:5122/"
    Check-RuntimeConfig "IPv6 loopback is bracketed" "::1" 5123 "::1" "http://[::1]:5123/"
    Check-RuntimeConfig "IPv6 wildcard maps to bracketed loopback" "::" 5124 "::1" "http://[::1]:5124/"
    Check-RuntimeConfig "IPv6 mapped literal is bracketed" "::ffff:127.0.0.1" 5125 "::ffff:127.0.0.1" "http://[::ffff:127.0.0.1]:5125/"
} finally {
    $env:APP_HOST = $oldHost
    $env:APP_PORT = $oldPort
}

if ($failures -ne 0) {
    Write-Host "`nSummary: FAIL ($failures checks failed)"
    exit 1
}
Write-Host "`nSummary: PASS"
exit 0
