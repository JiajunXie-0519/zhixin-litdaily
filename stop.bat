@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "data\runtime.json" (
  echo 知新没有在运行。
  ping -n 2 127.0.0.1 >nul
  exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $r=Get-Content -Raw 'data\runtime.json' | ConvertFrom-Json; try { Invoke-RestMethod -Method Post -Uri ($r.url + '/api/shutdown') -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null; Write-Host '知新已退出。' } catch { Write-Host '无法连接知新，可能已经退出。' }"

ping -n 2 127.0.0.1 >nul
exit /b 0
