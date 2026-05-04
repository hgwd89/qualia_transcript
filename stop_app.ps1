$ErrorActionPreference = "Stop"

$Port = 5000
$RootUrl = "http://127.0.0.1:5000/"
$SettingsUrl = "http://127.0.0.1:5000/settings"

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

function Test-QualiaAppEndpoint {
    try {
        $root = Invoke-WebRequest -Uri $RootUrl -UseBasicParsing -TimeoutSec 2
        if ([int]$root.StatusCode -ne 200) { return $false }

        $settings = Invoke-WebRequest -Uri $SettingsUrl -UseBasicParsing -TimeoutSec 2
        if ([int]$settings.StatusCode -ne 200) { return $false }

        $content = "$($settings.Content)"
        return ($content -match "OpenAI APIキー|Whisperモデル|設定")
    } catch {
        return $false
    }
}

$target = Get-PortProcessInfo -LocalPort $Port
if (-not $target) {
    Write-Host "ポート $Port を使用中のプロセスはありません。停止対象なし。" -ForegroundColor Yellow
    exit 0
}

$looksLikeQualiaProcess = Is-QualiaFlaskProcess -ProcessInfo $target
$looksLikeQualiaEndpoint = Test-QualiaAppEndpoint

if (-not ($looksLikeQualiaProcess -or $looksLikeQualiaEndpoint)) {
    Write-Host "ポート $Port は使用中ですが、Qualia Transcript の Flask ではないため停止しません。" -ForegroundColor Red
    Write-Host "PID: $($target.Pid), Name: $($target.Name)" -ForegroundColor Red
    if ($target.CommandLine) {
        Write-Host "CommandLine: $($target.CommandLine)" -ForegroundColor Red
    }
    exit 1
}

Write-Host "停止対象:" -ForegroundColor Yellow
Write-Host "PID: $($target.Pid), Name: $($target.Name)" -ForegroundColor Yellow
if ($target.CommandLine) {
    Write-Host "CommandLine: $($target.CommandLine)" -ForegroundColor DarkYellow
}

Write-Host "Qualia Transcript を停止します (PID: $($target.Pid))..." -ForegroundColor Cyan
Stop-Process -Id $target.Pid -ErrorAction SilentlyContinue

for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    $after = Get-PortProcessInfo -LocalPort $Port
    if (-not $after) {
        Write-Host "停止成功。" -ForegroundColor Green
        exit 0
    }

    # Flask のリローダー等で残る場合のみ、同一条件のポート5000プロセスを追加停止
    if (Is-QualiaFlaskProcess -ProcessInfo $after) {
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
