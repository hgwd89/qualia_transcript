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
    throw "Python executable was not found. Check Python or the project virtual environment."
}

function Get-QualiaAppScriptPath {
    param([string]$ProjectDir)

    $scriptPath = Join-Path $ProjectDir "app.py"
    if (-not (Test-Path $scriptPath -PathType Leaf)) {
        throw "Qualia Transcript app.py was not found: $scriptPath"
    }
    return (Resolve-Path $scriptPath).Path
}

function Test-QualiaProcessCommandLine {
    param(
        [string]$CommandLine,
        [string]$ProjectDir,
        [string]$ProcessName = ""
    )

    if (-not $CommandLine) { return $false }
    if ($ProcessName -and -not $ProcessName.ToLowerInvariant().StartsWith("python")) {
        return $false
    }
    try {
        $appScript = Get-QualiaAppScriptPath -ProjectDir $ProjectDir
    } catch {
        return $false
    }

    $normalizedLine = "$CommandLine".Replace("/", "\").ToLowerInvariant()
    $normalizedScript = "$appScript".Replace("/", "\").ToLowerInvariant()
    $escapedScript = [Regex]::Escape($normalizedScript)
    $argumentPattern = '(?:^|\s)(?:"' + $escapedScript + '"|' + $escapedScript + ')(?=\s|$)'
    return [Regex]::IsMatch($normalizedLine, $argumentPattern)
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

function Test-QualiaSettingsContent {
    param(
        [string]$Content,
        [string]$ServiceName
    )

    if (-not $Content -or -not $ServiceName) { return $false }
    $decodedContent = [System.Net.WebUtility]::HtmlDecode("$Content")
    return (
        $decodedContent.Contains($ServiceName) -and
        ($decodedContent -match "OpenAI APIキー|Whisperモデル")
    )
}

function Get-QualiaRequestTimeoutSec {
    param(
        [datetime]$DeadlineUtc,
        [int]$DefaultTimeoutSec = 2
    )

    if ($DefaultTimeoutSec -lt 1) { return 0 }
    $remainingSeconds = ($DeadlineUtc - [DateTime]::UtcNow).TotalSeconds
    if ($remainingSeconds -lt 1) { return 0 }
    return [int][Math]::Min($DefaultTimeoutSec, [Math]::Floor($remainingSeconds))
}

function Test-QualiaAppEndpoint {
    param(
        [string]$RootUrl,
        [string]$ServiceName,
        [int]$TimeoutSec = 2,
        [datetime]$DeadlineUtc = [datetime]::MaxValue
    )

    if (-not $RootUrl -or -not $ServiceName) { return $false }
    try {
        $rootTimeout = Get-QualiaRequestTimeoutSec `
            -DeadlineUtc $DeadlineUtc `
            -DefaultTimeoutSec $TimeoutSec
        if ($rootTimeout -lt 1) { return $false }
        $root = Invoke-WebRequest -Uri $RootUrl -UseBasicParsing -TimeoutSec $rootTimeout
        if ([int]$root.StatusCode -ne 200) { return $false }

        $settingsTimeout = Get-QualiaRequestTimeoutSec `
            -DeadlineUtc $DeadlineUtc `
            -DefaultTimeoutSec $TimeoutSec
        if ($settingsTimeout -lt 1) { return $false }
        $settingsUrl = "${RootUrl}settings"
        $settings = Invoke-WebRequest -Uri $settingsUrl -UseBasicParsing -TimeoutSec $settingsTimeout
        if ([int]$settings.StatusCode -ne 200) { return $false }

        return Test-QualiaSettingsContent `
            -Content "$($settings.Content)" `
            -ServiceName $ServiceName
    } catch {
        return $false
    }
}

function Get-QualiaRuntimeConfig {
    param(
        [string]$ProjectDir,
        [string]$PythonExe
    )

    Push-Location $ProjectDir
    try {
        $json = & $PythonExe -c "import json, config; print(json.dumps({'host': config.APP_HOST, 'port': config.APP_PORT, 'service_name': config.SERVICE_NAME}))"
        if ($LASTEXITCODE -ne 0 -or -not $json) {
            throw "Could not load APP_HOST / APP_PORT from config.py."
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
        ServiceName = "$($runtime.service_name)"
    }
}
