# Fleet scaling guide

Phase-wise targets for running multiple isolated Facebook bots.

## Phase 1 — Windows PC (5–10 bots)

**Goal:** Verify login, proxy, quotas, and crash isolation on one machine.

| Setting | Recommendation |
|---------|----------------|
| RAM | 16 GB minimum |
| Mode | `--mode structured` (default in fleet launcher) |
| Browser | `--headless` |
| Startup | Staggered 30–120 s between bots |
| Proxy | One residential/mobile proxy per account |

```bash
python scripts/fleet_launcher.py --max-bots 10 --phase 1
```

## Phase 2 — Single server (20–50 bots)

**Goal:** Stable daily operation with monitoring and auto-restart.

| Setting | Recommendation |
|---------|----------------|
| RAM | 32–64 GB |
| Ollama | Shared pool or stay on structured mode |
| Disk | ~10 GB for profiles |
| Alerts | Set `FLEET_ALERT_WEBHOOK` for checkpoint/crash |

```bash
python scripts/fleet_launcher.py --max-bots 50 --phase 2
```

## Phase 3 — Distributed (500+ bots)

**Goal:** Many machines, each running 40–50 containers.

| Setting | Recommendation |
|---------|----------------|
| Machines | 10–15 × 64 GB RAM servers |
| Bots per machine | 40–50 |
| Orchestration | Docker Compose per host or Kubernetes |
| LLM | Structured mode or dedicated Ollama/GPU cluster |
| Secrets | Per-container env mount from `accounts/` |

```bash
# On each host (example: 50 bots)
docker compose -f docker-compose.yml up --scale bot=50
```

## Resource estimates

| Bots | RAM (headless) | CPU cores |
|------|----------------|-----------|
| 10 | ~4–6 GB | 4+ |
| 50 | ~20–30 GB | 8+ |
| 500 | ~125–250 GB total | Distributed |

## Commands

```bash
# Migrate legacy cookies.txt → accounts/accounts.json
python scripts/migrate_cookies_to_registry.py

# Launch fleet (structured, headless, staggered)
python scripts/fleet_launcher.py

# View all bot statuses
python scripts/fleet_launcher.py --status

# Single bot with proxy
python scripts/run_agent_brain.py --account-id ID --proxy http://user:pass@host:port --headless --close-on-exit
```
