param(
    [switch]$IntegratedPreview,
    [switch]$Mapping,
    [switch]$Analysis,
    [switch]$Transcription,
    [switch]$Outputs,
    [switch]$AllPaid
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

$hasAnyFlag = $IntegratedPreview -or $Mapping -or $Analysis -or $Transcription -or $Outputs -or $AllPaid

if (-not $hasAnyFlag) {
    Invoke-Check -Name "Safe Smoke Check" -ScriptPath (Join-Path $scriptDir "check_safe.ps1") -Paid:$false
    Write-Host "[PASS] check_all completed (safe only)."
    exit 0
}

if ($Mapping) {
    Invoke-Check -Name "Mapping Smoke Check" -ScriptPath (Join-Path $scriptDir "check_mapping.ps1") -Paid:$true
}
if ($IntegratedPreview) {
    Invoke-Check -Name "Integrated Analysis Preview UI Check" -ScriptPath (Join-Path $scriptDir "check_integrated_analysis_preview_ui.ps1") -Paid:$false
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
