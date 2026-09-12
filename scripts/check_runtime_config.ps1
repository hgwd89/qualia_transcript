$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
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

function Assert-Bool {
    param(
        [string]$Name,
        [bool]$Actual,
        [bool]$Expected
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

$appScript = Get-QualiaAppScriptPath -ProjectDir $projectRoot
$exactCommand = "`"C:\Python\python.exe`" `"$appScript`""
Assert-Bool -Name "exact repository app.py command line is recognized" `
    -Actual (Test-QualiaProcessCommandLine -CommandLine $exactCommand -ProjectDir $projectRoot -ProcessName "python") `
    -Expected $true

Assert-Bool -Name "exact app.py path under a non-Python process is rejected" `
    -Actual (Test-QualiaProcessCommandLine -CommandLine $exactCommand -ProjectDir $projectRoot -ProcessName "cmd") `
    -Expected $false

Assert-Bool -Name "generic relative python app.py is not enough for process identity" `
    -Actual (Test-QualiaProcessCommandLine -CommandLine "python app.py" -ProjectDir $projectRoot -ProcessName "python") `
    -Expected $false

Assert-Bool -Name "another repository app.py is rejected" `
    -Actual (Test-QualiaProcessCommandLine -CommandLine '"C:\Python\python.exe" "C:\other-project\app.py"' -ProjectDir $projectRoot -ProcessName "python") `
    -Expected $false

Assert-Bool -Name "Qualia settings content requires service identity and marker" `
    -Actual (Test-QualiaSettingsContent -Content '<title>設定 | Qualia Transcript</title><div>Whisperモデル</div>' -ServiceName "Qualia Transcript") `
    -Expected $true

Assert-Bool -Name "generic settings page is not accepted as Qualia" `
    -Actual (Test-QualiaSettingsContent -Content '<title>設定</title><div>Whisperモデル</div>' -ServiceName "Qualia Transcript") `
    -Expected $false

Assert-Bool -Name "service name alone is insufficient without a Qualia settings marker" `
    -Actual (Test-QualiaSettingsContent -Content '<title>Qualia Transcript</title>' -ServiceName "Qualia Transcript") `
    -Expected $false

$startScriptText = Get-Content (Join-Path $projectRoot "start_app.ps1") -Raw
$stopScriptText = Get-Content (Join-Path $projectRoot "stop_app.ps1") -Raw
Assert-Bool -Name "launcher passes absolute app.py identity to Python" `
    -Actual ($startScriptText.Contains("Get-QualiaAppScriptPath") -and $startScriptText.Contains('$AppArgument')) `
    -Expected $true
Assert-Bool -Name "forced stop is fenced to the original listener PID" `
    -Actual ($stopScriptText.Contains('$after.Pid -eq $initialPid')) `
    -Expected $true

if ($failures -gt 0) {
    Write-Host "`nSummary: FAIL ($failures checks failed)"
    exit 1
}

Write-Host "`nSummary: PASS"
exit 0
