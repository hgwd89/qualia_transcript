$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
. (Join-Path $ProjectDir "scripts\runtime_config.ps1")

$pythonExe = Get-QualiaPythonExecutable -ProjectDir $ProjectDir
$runtime = Get-QualiaRuntimeConfig -ProjectDir $ProjectDir -PythonExe $pythonExe
$Port = $runtime.Port
$RootUrl = $runtime.Url
$ServiceName = $runtime.ServiceName

function Get-PortProcessInfo {
    param([int]$LocalPort)

    $conn = Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }

    $ownerPid = $conn.OwningProcess
    $proc = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    $wmi = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" -ErrorAction SilentlyContinue

    [PSCustomObject]@{
        Pid         = $ownerPid
        Name        = if ($proc) { $proc.ProcessName } else { "" }
        CommandLine = if ($wmi) { $wmi.CommandLine } else { "" }
    }
}

function Is-QualiaFlaskProcess {
    param([object]$ProcessInfo)
    if (-not $ProcessInfo) { return $false }
    return Test-QualiaProcessCommandLine `
        -CommandLine "$($ProcessInfo.CommandLine)" `
        -ProjectDir $ProjectDir `
        -ProcessName "$($ProcessInfo.Name)"
}

$target = Get-PortProcessInfo -LocalPort $Port
if (-not $target) {
    Write-Host "ポート $Port を使用中のプロセスはありません。停止対象なし。" -ForegroundColor Yellow
    exit 0
}

$looksLikeQualiaProcess = Is-QualiaFlaskProcess -ProcessInfo $target
$looksLikeQualiaEndpoint = Test-QualiaAppEndpoint `
    -RootUrl $RootUrl `
    -ServiceName $ServiceName `
    -TimeoutSec 2

if (-not ($looksLikeQualiaProcess -or $looksLikeQualiaEndpoint)) {
    Write-Host "ポート $Port は使用中ですが、Qualia Transcript と確認できないため停止しません。" -ForegroundColor Red
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

Write-Host "Qualia Transcript を停止します (PID: $initialPid)..." -ForegroundColor Cyan
Stop-Process -Id $initialPid -ErrorAction SilentlyContinue

for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    $after = Get-PortProcessInfo -LocalPort $Port
    if (-not $after) {
        Write-Host "停止成功。" -ForegroundColor Green
        exit 0
    }

    # Force only the same listener we originally identified, and only when its
    # command line still points at this repository's absolute app.py path.
    if ([int]$after.Pid -eq $initialPid -and (Is-QualiaFlaskProcess -ProcessInfo $after)) {
        Stop-Process -Id $after.Pid -Force -ErrorAction SilentlyContinue
    }
}

$final = Get-PortProcessInfo -LocalPort $Port
Write-Host "停止確認に失敗しました。まだポート $Port が使用中です。" -ForegroundColor Red
Write-Host "PID: $($final.Pid), Name: $($final.Name)" -ForegroundColor Red
if ($final.CommandLine) {
    Write-Host "CommandLine: $($final.CommandLine)" -ForegroundColor Red
}
exit 1
