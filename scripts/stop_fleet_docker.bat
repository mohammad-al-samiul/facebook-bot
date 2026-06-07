@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

echo Stopping all bot containers...
docker compose -f docker-compose.fleet.yml down
if errorlevel 1 (
  echo Stop failed. Is the fleet running?
  pause
  exit /b 1
)

echo All bots stopped. Sessions remain in profiles\ folder.
pause
