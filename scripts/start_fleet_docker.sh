#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "============================================"
echo " Facebook Bot Fleet - Docker One-Click Start"
echo "============================================"

command -v docker >/dev/null || { echo "Docker not installed"; exit 1; }
docker info >/dev/null || { echo "Docker daemon not running"; exit 1; }

python scripts/docker_fleet_compose.py
docker build -t bot-agent:fleet .
docker compose -f docker-compose.fleet.yml up -d

echo "Fleet started. Sessions persist in ./profiles/<account_id>/"
docker compose -f docker-compose.fleet.yml ps
