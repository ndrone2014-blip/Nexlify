@echo off
setlocal
cd /d "%~dp0"
title Nexora installer

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer was not found.
  echo Install Python 3.12+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Failed to create the virtual environment. & pause & exit /b 1)
)

echo Installing packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Package installation failed. & pause & exit /b 1)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  ".venv\Scripts\python.exe" -c "import secrets,re,pathlib; p=pathlib.Path('.env'); t=p.read_text(); p.write_text(re.sub(r'^SESSION_SECRET=.*$', 'SESSION_SECRET='+secrets.token_urlsafe(48), t, flags=re.M))"
  echo Created .env with a random session secret.
)

if not exist "data" mkdir data

echo.
echo Done! Next steps:
echo   1. Open .env and fill in DISCORD_BOT_TOKEN, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET and BOT_OWNER_IDS.
echo   2. In the Discord Developer Portal, add http://localhost:8000/auth/callback as an OAuth2 redirect
echo      and enable the Server Members and Message Content intents.
echo   3. Run start.bat
echo.
pause
