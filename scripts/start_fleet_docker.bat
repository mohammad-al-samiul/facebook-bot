@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

echo ============================================
echo  Facebook Bot Fleet - Docker One-Click Start
echo ============================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
  echo Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is installed but not running. Start Docker Desktop first.
  pause
  exit /b 1
)

if not exist "cookies.txt" (
  if not exist "accounts\accounts.json" (
    echo Missing cookies.txt and accounts\accounts.json
    pause
    exit /b 1
  )
)

echo [1/4] Generating docker-compose.fleet.yml from your accounts...
python scripts\docker_fleet_compose.py
if errorlevel 1 (
  echo Failed to generate compose file.
  pause
  exit /b 1
)

echo [2/5] Building bot image (first run may take a few minutes)...
docker build -t bot-agent:fleet .
if errorlevel 1 (
  echo Docker build failed.
  pause
  exit /b 1
)

echo [3/5] Stopping old fleet containers (if any)...
docker compose -f docker-compose.fleet.yml down --remove-orphans 2>nul

echo [4/5] Starting all bot containers in background...
docker compose -f docker-compose.fleet.yml up -d
if errorlevel 1 (
  echo docker compose up failed.
  pause
  exit /b 1
)

echo [5/5] Fleet is up.
echo.
echo  - First run: each bot logs in and saves session to profiles\^<account_id^>\
echo  - Next runs: bots reuse saved session (no re-login)
echo  - Status:    python scripts\fleet_launcher.py --status
echo  - Logs:      docker compose -f docker-compose.fleet.yml logs -f
echo  - Stop all:  scripts\stop_fleet_docker.bat
echo.
docker compose -f docker-compose.fleet.yml ps
echo.
pause
