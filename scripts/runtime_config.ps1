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

    # Bind-all addresses are not valid browser destinations. Use the matching
    # loopback family for readiness checks and browser launch.
    if ($hostAddress -eq "0.0.0.0") {
        $browserHost = "127.0.0.1"
    } elseif ($hostAddress -eq "::") {
        $browserHost = "::1"
    } else {
        $browserHost = $hostAddress
    }

    # RFC 3986 requires IPv6 literals to be bracketed in URLs. Keep BrowserHost
    # unbracketed for diagnostics and expose only the URL-formatted host here.
    $urlHost = if ($browserHost.Contains(":")) { "[$browserHost]" } else { $browserHost }

    return [PSCustomObject]@{
        Host        = $hostAddress
        Port        = $port
        BrowserHost = $browserHost
        Url         = "http://${urlHost}:${port}/"
    }
}
