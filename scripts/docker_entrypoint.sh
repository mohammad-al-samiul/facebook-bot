#!/usr/bin/env bash
# Stagger container startup so all bots do not login at the same second.
set -euo pipefail

ACCOUNT_ID="${ACCOUNT_ID:-}"
STAGGER_MIN="${FLEET_STAGGER_MIN:-15}"
STAGGER_MAX="${FLEET_STAGGER_MAX:-90}"

if [[ -n "$ACCOUNT_ID" && "$STAGGER_MAX" != "0" ]]; then
  suffix="${ACCOUNT_ID: -4}"
  suffix="${suffix//[^0-9]/}"
  if [[ -z "$suffix" ]]; then suffix="0"; fi
  span=$(( STAGGER_MAX - STAGGER_MIN + 1 ))
  delay=$(( STAGGER_MIN + suffix % span ))
  echo "[fleet] account=$ACCOUNT_ID stagger=${delay}s before start"
  sleep "$delay"
fi

if [[ -z "$ACCOUNT_ID" ]]; then
  echo "ACCOUNT_ID is required" >&2
  exit 1
fi

exec python scripts/run_agent_brain.py \
  --account-id "$ACCOUNT_ID" \
  --fleet-mode \
  --headless \
  --close-on-exit \
  --mode "${FLEET_AGENT_MODE:-structured}" \
  --registry-file /app/accounts/accounts.json \
  --cookies-file /app/cookies.txt
