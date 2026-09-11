$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $projectRoot

python tests/smoke_interview_job_ui.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
exit 0
