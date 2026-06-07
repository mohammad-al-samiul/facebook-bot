@echo off
REM One-time setup for Windows (run from project root)
cd /d "%~dp0.."

REM Remove broken venv (e.g. moved project from Downloads to Documents)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo Removing broken .venv from old path...
    rmdir /s /q .venv
  )
)

if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv — install Python 3.10+ first.
    pause
    exit /b 1
  )
)

set PY=%CD%\.venv\Scripts\python.exe
echo Using: %PY%

"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
"%PY%" -m playwright install chromium

echo.
echo Setup done.
echo   Activate:  .venv\Scripts\activate
echo   Migrate:   python scripts\migrate_cookies_to_registry.py
echo   Test bot:  python scripts\run_agent_brain.py --account-id YOUR_ID
echo   All bots:  scripts\start_fleet_docker.bat
pause
