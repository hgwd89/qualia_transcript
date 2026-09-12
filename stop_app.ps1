$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
. (Join-Path $ProjectDir "scripts\runtime_config.ps1")

$pythonExe = Get-QualiaPythonExecutable -ProjectDir $ProjectDir
$runtime = Get-QualiaRuntimeConfig -ProjectDir $ProjectDir -PythonExe $pythonExe
$Port = $runtime.Port

function Get-ProcessInfoById {
    param([int]$ProcessId)

    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }

    $startTimeUtcTicks = $null
    try {
        $startTimeUtcTicks = [long]$proc.StartTime.ToUniversalTime().Ticks
    } catch {
        return $null
    }

    $wmi = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $wmi) { return $null }

    # Re-read the process after the CIM lookup. If the PID was reused while the
    # command line was being read, the start time changes and the snapshot is
    # rejected rather than combining metadata from two process instances.
    $verifiedProc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $verifiedProc) { return $null }
    try {
        $verifiedStartTimeUtcTicks = [long]$verifiedProc.StartTime.ToUniversalTime().Ticks
    } catch {
        return $null
    }
    if ($verifiedStartTimeUtcTicks -ne $startTimeUtcTicks) { return $null }

    [PSCustomObject]@{
        Pid               = $ProcessId
        Name              = $verifiedProc.ProcessName
        CommandLine       = $wmi.CommandLine
        StartTimeUtcTicks = $verifiedStartTimeUtcTicks
        ProcessObject     = $verifiedProc
    }
}

function Get-PortProcessInfo {
    param([int]$LocalPort)

    $conn = Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    return Get-ProcessInfoById -ProcessId ([int]$conn.OwningProcess)
}

function Is-QualiaFlaskProcess {
    param([object]$ProcessInfo)
    if (-not $ProcessInfo) { return $false }
    return Test-QualiaProcessCommandLine `
        -CommandLine "$($ProcessInfo.CommandLine)" `
        -ProjectDir $ProjectDir `
        -ProcessName "$($ProcessInfo.Name)"
}

function Is-SameQualiaProcessInstance {
    param(
        [object]$Candidate,
        [object]$Initial
    )
    return Test-QualiaSameProcessInstance `
        -Candidate $Candidate `
        -Initial $Initial `
        -ProjectDir $ProjectDir
}

$target = Get-PortProcessInfo -LocalPort $Port
if (-not $target) {
    Write-Host "ポート $Port を使用中のプロセスはありません。停止対象なし。" -ForegroundColor Yellow
    exit 0
}

if (-not (Is-QualiaFlaskProcess -ProcessInfo $target) -or $null -eq $target.StartTimeUtcTicks) {
    Write-Host "ポート $Port は使用中ですが、このリポジトリの Qualia Transcript プロセスと安全に確認できないため停止しません。" -ForegroundColor Red
    Write-Host "PID: $($target.Pid), Name: $($target.Name)" -ForegroundColor Red
    if ($target.CommandLine) {
        Write-Host "CommandLine: $($target.CommandLine)" -ForegroundColor Red
    }
    exit 1
}

$initialPid = [int]$target.Pid
Write-Host "停止対象:" -ForegroundColor Yellow
Write-Host "PID: $initialPid, Name: $($target.Name)" -ForegroundColor Yellow
if ($target.CommandLine) {
    Write-Host "CommandLine: $($target.CommandLine)" -ForegroundColor DarkYellow
}

# Re-read the PID immediately before the first termination signal. If the
# original process already exited and Windows reused the PID, do not touch the
# replacement process.
$preStop = Get-ProcessInfoById -ProcessId $initialPid
if (-not $preStop) {
    Write-Host "停止対象は停止操作前に既に終了したか、安全に再確認できませんでした。" -ForegroundColor Green
    exit 0
}
if (-not (Is-SameQualiaProcessInstance -Candidate $preStop -Initial $target)) {
    Write-Host "停止操作前に PID のプロセス identity が変化したため停止しません。" -ForegroundColor Red
    exit 1
}

Write-Host "Qualia Transcript を停止します (PID: $initialPid)..." -ForegroundColor Cyan
Stop-Process -InputObject $preStop.ProcessObject -ErrorAction SilentlyContinue

for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    $after = Get-ProcessInfoById -ProcessId $initialPid
    if (-not $after) {
        Write-Host "停止成功。" -ForegroundColor Green
        exit 0
    }

    # A reused PID is a different process instance even when the number matches.
    # Never force-stop it. Force is allowed only while PID, start time, and exact
    # repository app.py argument still identify the original process instance.
    if (-not (Is-SameQualiaProcessInstance -Candidate $after -Initial $target)) {
        Write-Host "元の Qualia Transcript は終了しました。再利用された PID のプロセスは停止しません。" -ForegroundColor Green
        exit 0
    }

    Stop-Process -InputObject $after.ProcessObject -Force -ErrorAction SilentlyContinue
}

$final = Get-ProcessInfoById -ProcessId $initialPid
if (-not $final -or -not (Is-SameQualiaProcessInstance -Candidate $final -Initial $target)) {
    Write-Host "停止成功。" -ForegroundColor Green
    exit 0
}

Write-Host "停止確認に失敗しました。元の Qualia Transcript プロセスがまだ実行中です。" -ForegroundColor Red
Write-Host "PID: $($final.Pid), Name: $($final.Name)" -ForegroundColor Red
if ($final.CommandLine) {
    Write-Host "CommandLine: $($final.CommandLine)" -ForegroundColor Red
}
exit 1
