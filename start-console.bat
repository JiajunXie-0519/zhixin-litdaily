@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 知新 · 调试控制台
echo.
echo   知新 · 调试模式
echo   关闭这个窗口或按 Ctrl+C 停止服务。
echo.
python app.py
if errorlevel 1 (
  echo.
  echo 服务退出，错误信息见上方和 data\server.log。
  pause
)
