@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYW="
for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%P"
if not defined PYW (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYW if exist "%%~dpPpythonw.exe" set "PYW=%%~dpPpythonw.exe"
  )
)

if not defined PYW (
  echo [错误] 没有找到 Python。请先安装 Python 3.10 或更高版本：
  echo        https://www.python.org/downloads/
  echo        安装时请勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

start "" "%PYW%" "%~dp0app.py"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ok=$false; for($i=0; $i -lt 24; $i++){ Start-Sleep -Milliseconds 300; if(Test-Path 'data\runtime.json'){ try { $r=Get-Content -Raw 'data\runtime.json' | ConvertFrom-Json; $h=Invoke-RestMethod -Uri ($r.url + '/api/health') -TimeoutSec 2; if($h.app -eq '知新'){ $ok=$true; break } } catch {} } }; if(-not $ok){ exit 1 }"

if errorlevel 1 (
  echo.
  echo 知新没有正常启动。请查看 data\server.log，或运行 start-console.bat 查看错误。
  echo.
  pause
)
exit /b 0
