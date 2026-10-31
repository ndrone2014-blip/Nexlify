@echo off
cd /d "%~dp0"
title Nexora
if not exist ".venv\Scripts\python.exe" (
  echo Nexora isn't installed yet. Running install.bat first...
  call install.bat
)
if not exist ".env" (
  echo .env is missing. Run install.bat, then fill in .env.
  pause
  exit /b 1
)
echo Starting Nexora. The dashboard will be at http://localhost:8000
echo Press Ctrl+C to stop.
".venv\Scripts\python.exe" run.py
pause
