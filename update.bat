@echo off
cd /d "%~dp0"
title Nexora updater
where git >nul 2>nul
if errorlevel 1 (
  echo Git isn't installed, so download the latest code from GitHub manually, then run install.bat.
  pause
  exit /b 1
)
echo Backing up the database...
if exist "data\nexora.db" (
  if not exist "data\backups" mkdir "data\backups"
  copy "data\nexora.db" "data\backups\nexora-before-update.db" >nul
)
git pull || (echo Update failed. & pause & exit /b 1)
call install.bat
echo Updated. Database changes are applied automatically the next time you run start.bat.
