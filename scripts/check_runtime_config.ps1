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

Assert-Bool -Name "app.py path used only as a longer argument substring is rejected" `
    -Actual (Test-QualiaProcessCommandLine -CommandLine ("python `"$appScript.backup`"") -ProjectDir $projectRoot -ProcessName "python") `
    -Expected $false

Assert-Bool -Name "app.py path mentioned inside another argument is rejected" `
    -Actual (Test-QualiaProcessCommandLine -CommandLine ("python -c `"print('$appScript')`"") -ProjectDir $projectRoot -ProcessName "python") `
    -Expected $false

$spaceProjectDir = Join-Path $env:TEMP ("Qualia Launcher Space " + [guid]::NewGuid().ToString("N"))
try {
    New-Item -Path $spaceProjectDir -ItemType Directory -Force | Out-Null
    $spaceAppScript = Join-Path $spaceProjectDir "app.py"
    Set-Content -LiteralPath $spaceAppScript -Value "# launcher identity fixture" -Encoding UTF8
    $resolvedSpaceApp = (Resolve-Path $spaceAppScript).Path

    Assert-Bool -Name "quoted app.py path with spaces is one exact argv element" `
        -Actual (Test-QualiaProcessCommandLine `
            -CommandLine ("`"C:\Python\python.exe`" `"$resolvedSpaceApp`"") `
            -ProjectDir $spaceProjectDir `
            -ProcessName "python") `
        -Expected $true

    Assert-Bool -Name "unquoted path split across argv elements is rejected" `
        -Actual (Test-QualiaProcessCommandLine `
            -CommandLine ("`"C:\Python\python.exe`" $resolvedSpaceApp") `
            -ProjectDir $spaceProjectDir `
            -ProcessName "python") `
        -Expected $false

    Assert-Bool -Name "separate arguments that concatenate visually to app path are rejected" `
        -Actual (Test-QualiaProcessCommandLine `
            -CommandLine ("python other.py `"$($spaceProjectDir.Substring(0, $spaceProjectDir.LastIndexOf(' ')))`" `"$($spaceProjectDir.Substring($spaceProjectDir.LastIndexOf(' ') + 1))\app.py`"") `
            -ProjectDir $spaceProjectDir `
            -ProcessName "python") `
        -Expected $false
} finally {
    if (Test-Path $spaceProjectDir) {
        Remove-Item -LiteralPath $spaceProjectDir -Recurse -Force
    }
}

$initialProcess = [PSCustomObject]@{
    Pid = 4242
    Name = "python"
    CommandLine = $exactCommand
    StartTimeUtcTicks = 1000000
}
$sameProcess = [PSCustomObject]@{
    Pid = 4242
    Name = "python"
    CommandLine = $exactCommand
    StartTimeUtcTicks = 1000000
}
$reusedPid = [PSCustomObject]@{
    Pid = 4242
    Name = "python"
    CommandLine = $exactCommand
    StartTimeUtcTicks = 2000000
}
Assert-Bool -Name "same PID start-time and app identity is the same process instance" `
    -Actual (Test-QualiaSameProcessInstance -Candidate $sameProcess -Initial $initialProcess -ProjectDir $projectRoot) `
    -Expected $true
Assert-Bool -Name "reused PID with a different start time is rejected" `
    -Actual (Test-QualiaSameProcessInstance -Candidate $reusedPid -Initial $initialProcess -ProjectDir $projectRoot) `
    -Expected $false

Assert-Bool -Name "Qualia settings content requires service identity and fixed product markers" `
    -Actual (Test-QualiaSettingsContent -Content '<title>設定 | Qualia Transcript</title><div>Whisperモデル</div><div>MVP v0.1</div>' -ServiceName "Qualia Transcript") `
    -Expected $true

Assert-Bool -Name "HTML-escaped service name is decoded before identity comparison" `
    -Actual (Test-QualiaSettingsContent -Content '<title>Research &amp; Insights</title><div>Whisperモデル</div><div>MVP v0.1</div>' -ServiceName "Research & Insights") `
    -Expected $true

Assert-Bool -Name "empty configured service name falls back to fixed Qualia product markers" `
    -Actual (Test-QualiaSettingsContent -Content '<title>設定 | </title><div>Whisperモデル</div><div>MVP v0.1</div>' -ServiceName "") `
    -Expected $true

Assert-Bool -Name "empty service name does not accept a generic settings marker alone" `
    -Actual (Test-QualiaSettingsContent -Content '<title>設定</title><div>Whisperモデル</div>' -ServiceName "") `
    -Expected $false

Assert-Bool -Name "generic settings page is not accepted as Qualia" `
    -Actual (Test-QualiaSettingsContent -Content '<title>設定</title><div>Whisperモデル</div><div>MVP v0.1</div>' -ServiceName "Qualia Transcript") `
    -Expected $false

Assert-Bool -Name "service name alone is insufficient without fixed Qualia product markers" `
    -Actual (Test-QualiaSettingsContent -Content '<title>Qualia Transcript</title>' -ServiceName "Qualia Transcript") `
    -Expected $false

Assert-Equal -Name "expired readiness deadline refuses another request" `
    -Actual ([string](Get-QualiaRequestTimeoutSec -DeadlineUtc ([DateTime]::UtcNow.AddMilliseconds(-1)) -DefaultTimeoutSec 2)) `
    -Expected "0"

$startScriptText = Get-Content (Join-Path $projectRoot "start_app.ps1") -Raw
$stopScriptText = Get-Content (Join-Path $projectRoot "stop_app.ps1") -Raw
$runtimeScriptText = Get-Content (Join-Path $scriptDir "runtime_config.ps1") -Raw
Assert-Bool -Name "launcher passes absolute app.py identity to Python" `
    -Actual ($startScriptText.Contains("Get-QualiaAppScriptPath") -and $startScriptText.Contains('$AppArgument')) `
    -Expected $true
Assert-Bool -Name "process identity uses Windows argv parsing instead of raw path regex matching" `
    -Actual ($runtimeScriptText.Contains("CommandLineToArgvW") -and -not $runtimeScriptText.Contains('$argumentPattern =')) `
    -Expected $true
Assert-Bool -Name "readiness loop uses one wall-clock UTC deadline" `
    -Actual ($startScriptText.Contains('[DateTime]::UtcNow.AddSeconds($MaxSeconds)') -and $startScriptText.Contains('-DeadlineUtc $deadlineUtc')) `
    -Expected $true
Assert-Bool -Name "failed startup cleanup terminates through the Start-Process object" `
    -Actual ($startScriptText.Contains('Stop-Process -InputObject $launched') -and -not $startScriptText.Contains('Stop-Process -Id $launched.Id')) `
    -Expected $true
Assert-Bool -Name "stop requires exact repository process identity" `
    -Actual ($stopScriptText.Contains("Is-QualiaFlaskProcess") -and -not $stopScriptText.Contains("Test-QualiaAppEndpoint")) `
    -Expected $true
Assert-Bool -Name "normal stop revalidates the original process instance immediately before termination" `
    -Actual ($stopScriptText.Contains('$preStop = Get-ProcessInfoById') -and $stopScriptText.Contains('Is-SameQualiaProcessInstance -Candidate $preStop -Initial $target') -and $stopScriptText.Contains('Stop-Process -InputObject $preStop.ProcessObject')) `
    -Expected $true
Assert-Bool -Name "forced stop remains fenced to PID start-time and exact app identity" `
    -Actual ($stopScriptText.Contains('Is-SameQualiaProcessInstance -Candidate $after -Initial $target') -and $stopScriptText.Contains('StartTimeUtcTicks') -and $stopScriptText.Contains('Stop-Process -InputObject $after.ProcessObject -Force')) `
    -Expected $true

if ($failures -gt 0) {
    Write-Host "`nSummary: FAIL ($failures checks failed)"
    exit 1
}

Write-Host "`nSummary: PASS"
exit 0
