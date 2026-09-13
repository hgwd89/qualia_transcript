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

function Initialize-QualiaNativeCommandLineParser {
    if ("Qualia.NativeCommandLine" -as [type]) {
        return
    }

    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace Qualia {
    public static class NativeCommandLine {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr CommandLineToArgvW(
            string lpCmdLine,
            out int pNumArgs
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr LocalFree(IntPtr hMem);
    }
}
'@
}

function ConvertFrom-QualiaWindowsCommandLine {
    param([string]$CommandLine)

    if (-not $CommandLine) {
        return @()
    }

    Initialize-QualiaNativeCommandLineParser
    $argc = 0
    $argv = [Qualia.NativeCommandLine]::CommandLineToArgvW($CommandLine, [ref]$argc)
    if ($argv -eq [IntPtr]::Zero -or $argc -lt 1) {
        return @()
    }

    $arguments = @()
    try {
        for ($i = 0; $i -lt $argc; $i++) {
            $argumentPtr = [Runtime.InteropServices.Marshal]::ReadIntPtr(
                $argv,
                $i * [IntPtr]::Size
            )
            if ($argumentPtr -eq [IntPtr]::Zero) {
                return @()
            }
            $arguments += [Runtime.InteropServices.Marshal]::PtrToStringUni($argumentPtr)
        }
    } finally {
        [void][Qualia.NativeCommandLine]::LocalFree($argv)
    }
    return @($arguments)
}

function Normalize-QualiaCommandArgument {
    param([string]$Argument)

    return "$Argument".Replace("/", "\").ToLowerInvariant()
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
        $arguments = @(ConvertFrom-QualiaWindowsCommandLine -CommandLine $CommandLine)
    } catch {
        return $false
    }
    if ($arguments.Count -lt 2) {
        return $false
    }

    $normalizedScript = Normalize-QualiaCommandArgument -Argument $appScript
    foreach ($argument in $arguments) {
        if ((Normalize-QualiaCommandArgument -Argument $argument) -ceq $normalizedScript) {
            return $true
        }
    }
    return $false
}

function Test-QualiaSameProcessInstance {
    param(
        [object]$Candidate,
        [object]$Initial,
        [string]$ProjectDir
    )

    if (-not $Candidate -or -not $Initial) { return $false }
    if ([int]$Candidate.Pid -ne [int]$Initial.Pid) { return $false }
    if ($null -eq $Candidate.StartTimeUtcTicks -or $null -eq $Initial.StartTimeUtcTicks) {
        return $false
    }
    if ([long]$Candidate.StartTimeUtcTicks -ne [long]$Initial.StartTimeUtcTicks) {
        return $false
    }
    return Test-QualiaProcessCommandLine `
        -CommandLine "$($Candidate.CommandLine)" `
        -ProjectDir $ProjectDir `
        -ProcessName "$($Candidate.Name)"
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
        [AllowEmptyString()][string]$ServiceName
    )

    if (-not $Content) { return $false }
    $decodedContent = [System.Net.WebUtility]::HtmlDecode("$Content")
    $hasQualiaProductMarkers = (
        $decodedContent.Contains("MVP v0.1") -and
        ($decodedContent -match "OpenAI APIキー|Whisperモデル")
    )
    if (-not $hasQualiaProductMarkers) {
        return $false
    }

    # SERVICE_NAME can legally be configured as an empty environment value.
    # In that case the rendered title/nav cannot carry a name, so rely on two
    # independent fixed Qualia UI markers rather than rejecting a healthy app.
    if ([string]::IsNullOrEmpty($ServiceName)) {
        return $true
    }
    return $decodedContent.Contains($ServiceName)
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
        [AllowEmptyString()][string]$ServiceName,
        [int]$TimeoutSec = 2,
        [datetime]$DeadlineUtc = [datetime]::MaxValue
    )

    if (-not $RootUrl) { return $false }
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
