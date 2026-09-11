$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
. (Join-Path $ProjectDir "scripts\runtime_config.ps1")

$pythonExe = Get-QualiaPythonExecutable -ProjectDir $ProjectDir
$runtime = Get-QualiaRuntimeConfig -ProjectDir $ProjectDir -PythonExe $pythonExe
$AppUrl = $runtime.Url

Start-Process $AppUrl
Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
