param(
    [switch]$IntegratedPreview,
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

$doSafe = $false
$doFlags = $false
$doIntegrated = $false
$doIntegratedPreview = $IntegratedPreview
$doAnalysisReview = $false
$doProfessionalIntegrity = $false
$doLocalProduction = $false
$doProductionReadinessSmoke = $false
$doProcessingJobs = $false
$doJobAdmission = $false
$doJobFencing = $false
$doResultIdempotency = $false
$doJobRecovery = $false
$doProjectPipelineResilience = $false

if ($AllLocal) {
    $doSafe = $true
    $doFlags = $true
    $doIntegrated = $true
    $doIntegratedPreview = $true
    $doAnalysisReview = $true
    $doProfessionalIntegrity = $true
    $doLocalProduction = $true
    $doProductionReadinessSmoke = $true
    $doProcessingJobs = $true
    $doJobAdmission = $true
    $doJobFencing = $true
    $doResultIdempotency = $true
    $doJobRecovery = $true
    $doProjectPipelineResilience = $true
}

$hasAnyFlag = $IntegratedPreview -or $Mapping -or $Analysis -or $Transcription -or $Outputs -or $AllPaid -or $AllLocal

if (-not $hasAnyFlag) {
    $doSafe = $true
}

if ($doSafe) {
    Invoke-Check -Name "Safe Smoke Check" -ScriptPath (Join-Path $scriptDir "check_safe.ps1") -Paid:$false
}

if (-not $hasAnyFlag) {
    Write-Host "[PASS] check_all completed (safe only)."
    exit 0
}

if ($doFlags) {
    Invoke-Check -Name "Segment Flag Smoke Check" -ScriptPath (Join-Path $scriptDir "check_flags.ps1") -Paid:$false
    Invoke-Check -Name "Speaker Assignment Smoke Check" -ScriptPath (Join-Path $scriptDir "check_speaker_assignments.ps1") -Paid:$false
    Invoke-Check -Name "Output Flag Smoke Check" -ScriptPath (Join-Path $scriptDir "check_outputs_flags.ps1") -Paid:$false
}
if ($doIntegrated) {
    Invoke-Check -Name "Integrated Analysis No-AI Check" -ScriptPath (Join-Path $scriptDir "check_integrated_analysis.ps1") -Paid:$false
    Invoke-Check -Name "Integrated Analysis CLI Guard Check" -ScriptPath (Join-Path $scriptDir "check_integrated_analysis_cli_guards.ps1") -Paid:$false
}
if ($doAnalysisReview) {
    Invoke-Check -Name "AI Analysis Review/Export Check" -ScriptPath (Join-Path $scriptDir "check_analysis_review.ps1") -Paid:$false
}
if ($doProfessionalIntegrity) {
    Invoke-Check -Name "Professional Output/Data Integrity Check" -ScriptPath (Join-Path $scriptDir "check_professional_integrity.ps1") -Paid:$false
}
if ($doLocalProduction) {
    Invoke-Check -Name "Local Production/Backup Restore Check" -ScriptPath (Join-Path $scriptDir "check_local_production.ps1") -Paid:$false
}
if ($doProductionReadinessSmoke) {
    Invoke-Check -Name "Production Readiness Audit Smoke Check" -ScriptPath (Join-Path $scriptDir "check_production_readiness_smoke.ps1") -Paid:$false
}
if ($doProcessingJobs) {
    Invoke-Check -Name "Durable Processing Job Check" -ScriptPath (Join-Path $scriptDir "check_processing_jobs.ps1") -Paid:$false
}
if ($doJobAdmission) {
    Invoke-Check -Name "Atomic Job Admission Check" -ScriptPath (Join-Path $scriptDir "check_job_admission.ps1") -Paid:$false
}
if ($doJobFencing) {
    Invoke-Check -Name "Processing Job Fencing Check" -ScriptPath (Join-Path $scriptDir "check_job_fencing.ps1") -Paid:$false
}
if ($doResultIdempotency) {
    Invoke-Check -Name "Processing Result Idempotency Check" -ScriptPath (Join-Path $scriptDir "check_processing_result_idempotency.ps1") -Paid:$false
}
if ($doJobRecovery) {
    Invoke-Check -Name "Stale Processing Job Recovery Check" -ScriptPath (Join-Path $scriptDir "check_job_recovery.ps1") -Paid:$false
}
if ($doProjectPipelineResilience) {
    Invoke-Check -Name "Project Pipeline Resilience Check" -ScriptPath (Join-Path $scriptDir "check_project_pipeline_resilience.ps1") -Paid:$false
}
if ($Mapping) {
    Invoke-Check -Name "Mapping Smoke Check" -ScriptPath (Join-Path $scriptDir "check_mapping.ps1") -Paid:$true
}
if ($doIntegratedPreview) {
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
