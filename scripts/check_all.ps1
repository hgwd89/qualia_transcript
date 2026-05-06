param(
    [switch]$Flags,
    [switch]$Mapping,
    [switch]$Analysis,
    [switch]$Transcription,
    [switch]$Outputs,
    [switch]$AllPaid,
    [switch]$AllLocal
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

function Invoke-Check {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [bool]$Paid = $false
    )

    if ($Paid) {
        Write-Host "[WARN] Paid API check: $Name"
    } else {
        Write-Host "[INFO] Check: $Name"
    }

    & powershell -ExecutionPolicy Bypass -File $ScriptPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name failed (exit code: $LASTEXITCODE)."
        exit $LASTEXITCODE
    }
}

if ($AllPaid) {
    $Mapping = $true
    $Analysis = $true
    $Transcription = $true
    $Outputs = $true
}

if ($Flags) {
    $doFlags = $true
}

if ($AllLocal) {
    $doSafe = $true
    $doFlags = $true
} else {
    $doSafe = $false
    $doFlags = $doFlags -or $false
}

$hasAnyFlag = $Flags -or $Mapping -or $Analysis -or $Transcription -or $Outputs -or $AllPaid -or $AllLocal

if (-not $hasAnyFlag) {
    $doSafe = $true
}

if ($doSafe) {
    Invoke-Check -Name "Safe Smoke Check" -ScriptPath (Join-Path $scriptDir "check_safe.ps1") -Paid:$false
}

if ($doFlags) {
    Invoke-Check -Name "Segment Flag Smoke Check" -ScriptPath (Join-Path $scriptDir "check_flags.ps1") -Paid:$false
    Invoke-Check -Name "Speaker Assignment Smoke Check" -ScriptPath (Join-Path $scriptDir "check_speaker_assignments.ps1") -Paid:$false
    Invoke-Check -Name "Output Flag Smoke Check" -ScriptPath (Join-Path $scriptDir "check_outputs_flags.ps1") -Paid:$false
}

if (-not $hasAnyFlag) {
    Write-Host "[PASS] check_all completed (safe only)."
    exit 0
}

if ($Mapping) {
    Invoke-Check -Name "Mapping Smoke Check" -ScriptPath (Join-Path $scriptDir "check_mapping.ps1") -Paid:$true
}
if ($Analysis) {
    Invoke-Check -Name "Analysis Smoke Check" -ScriptPath (Join-Path $scriptDir "check_analysis.ps1") -Paid:$true
}
if ($Transcription) {
    Invoke-Check -Name "Transcription Smoke Check" -ScriptPath (Join-Path $scriptDir "check_transcription.ps1") -Paid:$true
}
if ($Outputs) {
    Invoke-Check -Name "Output Smoke Check" -ScriptPath (Join-Path $scriptDir "check_outputs.ps1") -Paid:$false
}

Write-Host "[PASS] check_all completed."
exit 0
