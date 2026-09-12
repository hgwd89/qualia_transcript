$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
. (Join-Path $ProjectDir "scripts\runtime_config.ps1")

$pythonExe = Get-QualiaPythonExecutable -ProjectDir $ProjectDir
$AppScript = Get-QualiaAppScriptPath -ProjectDir $ProjectDir
$runtime = Get-QualiaRuntimeConfig -ProjectDir $ProjectDir -PythonExe $pythonExe
$Port = $runtime.Port
$AppUrl = $runtime.Url
$ServiceName = $runtime.ServiceName
$LogsDir = Join-Path $ProjectDir "logs"
$OutLog = Join-Path $LogsDir "flask_out.log"
$ErrLog = Join-Path $LogsDir "flask_err.log"
$MaxWaitSeconds = 30

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

function Wait-AppReady {
    param(
        [string]$Url,
        [string]$ExpectedServiceName,
        [int]$MaxSeconds
    )

    $deadlineUtc = [DateTime]::UtcNow.AddSeconds($MaxSeconds)
    while ([DateTime]::UtcNow -lt $deadlineUtc) {
        if (Test-QualiaAppEndpoint `
            -RootUrl $Url `
            -ServiceName $ExpectedServiceName `
            -TimeoutSec 2 `
            -DeadlineUtc $deadlineUtc) {
            return $true
        }

        $remainingMs = [int][Math]::Floor(($deadlineUtc - [DateTime]::UtcNow).TotalMilliseconds)
        if ($remainingMs -le 0) { break }
        Start-Sleep -Milliseconds ([Math]::Min(1000, $remainingMs))
    }
    return $false
}

Set-Location $ProjectDir
New-Item -Path $LogsDir -ItemType Directory -Force | Out-Null

$existing = Get-PortProcessInfo -LocalPort $Port
$qualiaAlreadyOk = Test-QualiaAppEndpoint -RootUrl $AppUrl -ServiceName $ServiceName -TimeoutSec 2
if ($existing) {
    Write-Host "ポート $Port は使用中です。既存プロセスを確認します..." -ForegroundColor Yellow
    Write-Host "PID: $($existing.Pid), Name: $($existing.Name)" -ForegroundColor Yellow
    if ($existing.CommandLine) {
        Write-Host "CommandLine: $($existing.CommandLine)" -ForegroundColor DarkYellow
    }

    if ($qualiaAlreadyOk) {
        Write-Host "Qualia Transcript は既に起動中です。二重起動は行いません。" -ForegroundColor Green
        Start-Process $AppUrl
        Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
        exit 0
    }

    Write-Host "ポート $Port は使用中ですが、Qualia Transcript と確認できないため起動を中止します。" -ForegroundColor Red
    if (Test-Path $ErrLog) {
        Write-Host "---- tail: $ErrLog ----" -ForegroundColor DarkYellow
        Get-Content $ErrLog -Tail 20
    }
    exit 1
}

if ($qualiaAlreadyOk) {
    Write-Host "Qualia Transcript の応答を確認しました。既に起動中として扱い、二重起動しません。" -ForegroundColor Green
    Start-Process $AppUrl
    Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
    exit 0
}

Write-Host "Qualia Transcript を起動します..." -ForegroundColor Cyan
Write-Host "Project: $ProjectDir" -ForegroundColor DarkGray
Write-Host "Python: $pythonExe" -ForegroundColor DarkGray
Write-Host "App: $AppScript" -ForegroundColor DarkGray
Write-Host "URL: $AppUrl" -ForegroundColor DarkGray
$AppArgument = '"' + $AppScript + '"'
$launched = Start-Process -FilePath $pythonExe -ArgumentList $AppArgument -WorkingDirectory $ProjectDir -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru

$ready = Wait-AppReady -Url $AppUrl -ExpectedServiceName $ServiceName -MaxSeconds $MaxWaitSeconds
$started = Get-PortProcessInfo -LocalPort $Port

if ($ready) {
    if ($started) {
        Write-Host "起動成功 (PID: $($started.Pid), Qualia endpoint verified)" -ForegroundColor Green
    } else {
        Write-Host "起動成功 (Qualia endpoint verified)" -ForegroundColor Green
    }
    Write-Host "ログ: $OutLog / $ErrLog" -ForegroundColor Green
    Start-Process $AppUrl
    Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
    exit 0
}

Write-Host "起動に失敗しました（${MaxWaitSeconds}秒以内に Qualia Transcript endpoint を確認できませんでした）。" -ForegroundColor Red
if (Test-Path $ErrLog) {
    Write-Host "---- tail: $ErrLog ----" -ForegroundColor DarkYellow
    Get-Content $ErrLog -Tail 20
}
if ($launched) {
    try {
        $launched.Refresh()
        if (-not $launched.HasExited) {
            # Use the process object returned by Start-Process rather than resolving
            # its numeric PID again; a reused PID must never be terminated here.
            Stop-Process -InputObject $launched -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Host "起動失敗後の launcher process cleanup を安全に確認できませんでした。" -ForegroundColor Yellow
    }
}
exit 1
