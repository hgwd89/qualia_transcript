$ErrorActionPreference = "Stop"

$AppUrl = "http://127.0.0.1:5000/"
Start-Process $AppUrl
Write-Host "ブラウザで $AppUrl を開きました。" -ForegroundColor Green
