$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\hagawa.InsightFactory\qualia_transcript"
$LogsDir = Join-Path $ProjectDir "logs"
$OutLog = Join-Path $LogsDir "flask_out.log"
$ErrLog = Join-Path $LogsDir "flask_err.log"
$AppUrl = "http://127.0.0.1:5000/"
$Port = 5000
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

function Is-QualiaFlaskProcess {
    param([object]$ProcessInfo)
    if (-not $ProcessInfo) { return $false }
    $line = "$($ProcessInfo.CommandLine)".ToLowerInvariant()
    return ($line -like "*python*" -and $line -like "*app.py*")
}

function Test-AppHttp200 {
    param(
        [string]$Url,
        [int]$TimeoutSec = 2
    )

    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ([int]$resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Wait-AppReady {
    param(
        [string]$Url,
        [int]$MaxSeconds
    )

    for ($i = 1; $i -le $MaxSeconds; $i++) {
        if (Test-AppHttp200 -Url $Url -TimeoutSec 2) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-PythonExecutable {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }
    throw "python 実行ファイルが見つかりません。Python をインストールし、PATH を確認してください。"
}

Set-Location $ProjectDir
New-Item -Path $LogsDir -ItemType Directory -Force | Out-Null

$existing = Get-PortProcessInfo -LocalPort $Port
$httpAlreadyOk = Test-AppHttp200 -Url $AppUrl -TimeoutSec 2
if ($existing) {
    Write-Host "ポート $Port は使用中です。既存プロセスを確認します..." -ForegroundColor Yellow
    Write-Host "PID: $($existing.Pid), Name: $($existing.Name)" -ForegroundColor Yellow
    if ($existing.CommandLine) {
        Write-Host "CommandLine: $($existing.CommandLine)" -ForegroundColor DarkYellow
    }

    if ($httpAlreadyOk) {
        Write-Host "既に起動中です（HTTP 200 応答）。二重起動は行いません。" -ForegroundColor Green
        Start-Process $AppUrl
        Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
        exit 0
    }

    Write-Host "ポート $Port は使用中ですが、HTTP 200 が返りません。起動を中止します。" -ForegroundColor Red
    if (Test-Path $ErrLog) {
        Write-Host "---- tail: $ErrLog ----" -ForegroundColor DarkYellow
        Get-Content $ErrLog -Tail 20
    }
    exit 1
}

# ポート占有プロセスを取得できない環境でも、HTTP 200 なら既に起動中とみなす
if ($httpAlreadyOk) {
    Write-Host "HTTP 200 応答を確認しました。既に起動中として扱い、二重起動しません。" -ForegroundColor Green
    Start-Process $AppUrl
    Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
    exit 0
}

Write-Host "Qualia Transcript を起動します..." -ForegroundColor Cyan
$pythonExe = Get-PythonExecutable
Write-Host "Python: $pythonExe" -ForegroundColor DarkGray
$launched = Start-Process -FilePath $pythonExe -ArgumentList "app.py" -WorkingDirectory $ProjectDir -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru

$ready = Wait-AppReady -Url $AppUrl -MaxSeconds $MaxWaitSeconds
$started = Get-PortProcessInfo -LocalPort $Port

if ($ready) {
    if ($started) {
        Write-Host "起動成功 (PID: $($started.Pid), HTTP 200)" -ForegroundColor Green
    } else {
        Write-Host "起動成功 (HTTP 200)" -ForegroundColor Green
    }
    Write-Host "ログ: $OutLog / $ErrLog" -ForegroundColor Green
    Start-Process $AppUrl
    Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
    exit 0
}

Write-Host "起動に失敗しました（${MaxWaitSeconds}秒以内に HTTP 200 になりませんでした）。" -ForegroundColor Red
if (Test-Path $ErrLog) {
    Write-Host "---- tail: $ErrLog ----" -ForegroundColor DarkYellow
    Get-Content $ErrLog -Tail 20
}
if ($launched -and -not $launched.HasExited) {
    Stop-Process -Id $launched.Id -ErrorAction SilentlyContinue
}
exit 1
