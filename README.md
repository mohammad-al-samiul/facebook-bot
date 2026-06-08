# Facebook Agent

**Autonomous Facebook browser agent** powered by **Playwright** and a local **Ollama** LLM brain — with optional Gemini fallback, human-like typing, and multi-account fleet support.

> **Disclaimer:** You are responsible for complying with Facebook Terms of Service and applicable laws. Use at your own risk.

---

## About | সম্পর্কে

| | |
|---|---|
| **English** | This project automates human-like Facebook activity for one account at a time (or many in fleet mode): feed engagement, status posts, friend requests, and session persistence — all driven by a local Ollama model. |
| **বাংলা** | PC-তে চলা **local Ollama** দিয়ে Facebook-এ মানুষের মতো কাজ করে — ফিডে লাইক/কমেন্ট/শেয়ার, স্ট্যাটাস পোস্ট, ফ্রেন্ড রিকোয়েস্ট। চাইলে fleet বা Docker-এ অনেক account একসাথে চালানো যায়। |

**Version:** `0.2.0` · **Python:** 3.10+ · **Package:** `facebook-agent`

---

## System design documentation | সিস্টেম ডিজাইন ডকুমেন্টেশন

Full architecture, data flow, modules, and scaling guides:

| Language | Document |
|----------|----------|
| **English** | [Whole Project System Design (English)](docs/SYSTEM_DESIGN_EN.md) |
| **বাংলা** | [সিস্টেম ডিজাইন — বাংলায়](docs/SYSTEM_DESIGN_BN.md) |

Additional guide:

- [Fleet scaling phases (5 → 500+ bots)](docs/FLEET_SCALING.md)

---

## Quick start

### Windows (first time)

```bat
scripts\setup.bat
.venv\Scripts\activate
python scripts/check_ollama.py
python scripts/run_agent_brain.py
```

### Manual (all platforms)

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
cp accounts/accounts.json.example accounts/accounts.json
# Edit accounts/accounts.json with your credentials
python scripts/check_ollama.py
python scripts/run_agent_brain.py
```

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com/) running locally with `llama3.1:8b` (`ollama pull llama3.1:8b`)
- Chromium (installed via `playwright install chromium`)
- Docker Desktop (optional, for fleet containers)

### Account setup

Add accounts in `accounts/accounts.json` (recommended) or legacy `cookies.txt` (three lines per account: id, password, cookie string).

```bash
cp accounts/accounts.json.example accounts/accounts.json
# Or migrate legacy format:
python scripts/migrate_cookies_to_registry.py
```

If the browser fails to start (profile lock):

```bash
python scripts/unlock_browser_profile.py --kill-chrome
python scripts/run_agent_brain.py
```

---

## Daily behaviour

| Activity | Default quota | Notes |
|----------|---------------|-------|
| Friend requests | 3–4 / day | Profiles with ≥ 2,000 friends or followers |
| Status posts | 3–5 / day | Topics inferred from feed memory |
| Shares | 20 / day | Human-typed captions to own timeline |
| Feed engagement | Continuous | Like, comment, share during cycles |

---

## Project layout

```
bot-agent/
├── README.md
├── docs/
│   ├── SYSTEM_DESIGN_EN.md      # Full system design (English)
│   ├── SYSTEM_DESIGN_BN.md      # Full system design (Bangla)
│   └── FLEET_SCALING.md
├── scripts/
│   ├── run_agent_brain.py       # Main entry point
│   ├── send_one_friend.py
│   ├── fleet_launcher.py
│   ├── check_ollama.py
│   └── ...
├── playwright_automation/       # Core library
├── accounts/                    # Account credentials (gitignored)
├── profiles/                    # Runtime sessions & quotas (gitignored)
└── .env                         # Environment config (gitignored)
```

---

## Common commands

```bash
# Default brain mode (Ollama decides each action)
python scripts/run_agent_brain.py

# Specific account
python scripts/run_agent_brain.py --account-id YOUR_ID

# Structured mode + headless + proxy (fleet-style)
python scripts/run_agent_brain.py --mode structured --headless --proxy http://user:pass@host:port

# Skip friend requests
python scripts/run_agent_brain.py --skip-friends

# Friend requests only
python scripts/send_one_friend.py --account-id YOUR_ID
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--mode` | `brain` | `brain` = Ollama JSON decisions; `structured` = fixed pipeline |
| `--proxy` | — | Per-bot proxy URL (`PROXY_URL` env also supported) |
| `--fleet-mode` | off | Worker mode: headless, no manual checkpoint wait |
| `--headless` | off | Run Chromium headless |
| `--daily-friend-min/max` | 3 / 4 | Daily friend request range |
| `--daily-post-min/max` | 3 / 5 | Daily status post range |
| `--min-daily-shares` | 20 | Daily share target |
| `--skip-friends` | off | Skip friend tab activity |

---

## Fleet (multiple bots)

```bash
python scripts/migrate_cookies_to_registry.py   # optional: cookies.txt → accounts.json
python scripts/fleet_launcher.py --max-bots 10 --phase 1
python scripts/fleet_launcher.py --status
```

See [docs/FLEET_SCALING.md](docs/FLEET_SCALING.md) for phase-wise scaling (5 → 50 → 500+ bots).

### Docker fleet

**Windows:** double-click `scripts/start_fleet_docker.bat`

```bat
scripts\start_fleet_docker.bat    REM start all bots
scripts\stop_fleet_docker.bat     REM stop all bots
```

**Linux:**

```bash
bash scripts/start_fleet_docker.sh
```

Sessions persist in `profiles/<account_id>/` — first run logs in, later runs reuse the saved session.

---

## Environment

Copy `.env.example` to `.env`. Key variables:

| Variable | Purpose |
|----------|---------|
| `OLLAMA_HOST` | Local Ollama server (default `127.0.0.1:11434`) |
| `OLLAMA_MODEL` | Model name (default `llama3.1:8b`) |
| `GEMINI_API_KEY` | Optional fallback when Ollama is offline |
| `MIN_AUDIENCE_FRIEND_REQUEST` | Min friends/followers for friend sends (default `2000`) |
| `FLEET_ALERT_WEBHOOK` | Optional webhook for checkpoint/crash alerts |

See [docs/SYSTEM_DESIGN_EN.md](docs/SYSTEM_DESIGN_EN.md) § Configuration for the full list.

---

## Architecture at a glance

```
CLI (run_agent_brain.py)
        │
        ├── account_registry / session / login
        ├── agent_executor (cycles, quotas)
        └── BaseBot (Playwright + stealth)
                 │
                 ├── agent_brain → Ollama (decisions)
                 ├── ai_comment → Ollama / Gemini (text)
                 └── actions → Facebook UI
```

For diagrams, module tables, and sequence flows, see the [system design docs](docs/SYSTEM_DESIGN_EN.md).
