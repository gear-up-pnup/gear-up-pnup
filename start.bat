@echo off
chcp 65001 >nul
title MEB Career Bootcamp 2026 - Server
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python belum terpasang. Unduh dari https://www.python.org/downloads/ lalu jalankan ulang.
  pause
  exit /b 1
)
python server.py %*
pause
