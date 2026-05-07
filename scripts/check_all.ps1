param(
    [switch]$Flags,
    [switch]$Review,
    [switch]$Quotes,
    [switch]$Integrated,
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
if ($Review) {
    $doReview = $true
}
if ($Quotes) {
    $doQuotes = $true
}

if ($AllLocal) {
    $doSafe = $true
    $doFlags = $true
    $doReview = $true
    $doQuotes = $true
    $doIntegrated = $true
} else {
    $doSafe = $false
    $doFlags = $doFlags -or $false
    $doReview = $doReview -or $false
    $doQuotes = $doQuotes -or $false
    $doIntegrated = $Integrated -or $false
}

$hasAnyFlag = $Flags -or $Review -or $Quotes -or $Integrated -or $Mapping -or $Analysis -or $Transcription -or $Outputs -or $AllPaid -or $AllLocal

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

if ($doReview) {
    Invoke-Check -Name "Review Queue Smoke Check" -ScriptPath (Join-Path $scriptDir "check_review_queue.ps1") -Paid:$false
}

if ($doQuotes) {
    Invoke-Check -Name "Quote Candidate Smoke Check" -ScriptPath (Join-Path $scriptDir "check_quote_candidates.ps1") -Paid:$false
    Invoke-Check -Name "Quote Candidate UI Smoke Check" -ScriptPath (Join-Path $scriptDir "check_quote_candidates_ui.ps1") -Paid:$false
}

if ($doIntegrated) {
    Invoke-Check -Name "Integrated Analysis No-AI Check" -ScriptPath (Join-Path $scriptDir "check_integrated_analysis.ps1") -Paid:$false
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
