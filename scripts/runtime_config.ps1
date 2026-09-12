function Get-QualiaPythonExecutable {
    param([string]$ProjectDir)

    $venvCandidates = @(
        (Join-Path $ProjectDir ".venv\Scripts\python.exe"),
        (Join-Path $ProjectDir "venv\Scripts\python.exe")
    )
    foreach ($candidate in $venvCandidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }
    throw "python 実行ファイルが見つかりません。Python またはプロジェクトの仮想環境を確認してください。"
}

function Get-QualiaBrowserHost {
    param([string]$HostAddress)

    $value = "$HostAddress".Trim()
    if ($value.StartsWith("[") -and $value.EndsWith("]") -and $value.Length -ge 2) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    if ($value -eq "0.0.0.0") {
        return "127.0.0.1"
    }
    if ($value -eq "::" -or $value -eq "0:0:0:0:0:0:0:0") {
        return "::1"
    }
    return $value
}

function Format-QualiaUrlHost {
    param([string]$HostAddress)

    $browserHost = Get-QualiaBrowserHost -HostAddress $HostAddress
    if ($browserHost.Contains(":")) {
        return "[$browserHost]"
    }
    return $browserHost
}

function Get-QualiaAppUrl {
    param(
        [string]$HostAddress,
        [int]$Port
    )

    $urlHost = Format-QualiaUrlHost -HostAddress $HostAddress
    return "http://${urlHost}:${Port}/"
}

function Get-QualiaRuntimeConfig {
    param(
        [string]$ProjectDir,
        [string]$PythonExe
    )

    Push-Location $ProjectDir
    try {
        $json = & $PythonExe -c "import json, config; print(json.dumps({'host': config.APP_HOST, 'port': config.APP_PORT}))"
        if ($LASTEXITCODE -ne 0 -or -not $json) {
            throw "config.py から APP_HOST / APP_PORT を取得できませんでした。"
        }
        $runtime = $json | ConvertFrom-Json
    } finally {
        Pop-Location
    }

    $hostAddress = "$($runtime.host)"
    $port = [int]$runtime.port
    $browserHost = Get-QualiaBrowserHost -HostAddress $hostAddress

    return [PSCustomObject]@{
        Host        = $hostAddress
        Port        = $port
        BrowserHost = $browserHost
        Url         = Get-QualiaAppUrl -HostAddress $hostAddress -Port $port
    }
}
