# Facebook Agent

Autonomous Facebook browser agent powered by **Playwright** and a local **Ollama** brain.

> **Disclaimer:** You are responsible for complying with Facebook Terms of Service.

## Quick start

**Windows (first time):**

```bat
scripts\setup.bat
.venv\Scriptsctivate
python scripts/check_ollama.py
python scripts/run_agent_brain.py
```

**Manual:**

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
python scripts/check_ollama.py
python scripts/run_agent_brain.py
```

If the browser fails to start (profile lock):

```bash
python scripts/unlock_browser_profile.py --kill-chrome
python scripts/run_agent_brain.py
```

Add accounts in `accounts/accounts.json` (recommended) or legacy `cookies.txt` (three lines per account: id, password, cookie string).

```bash
cp accounts/accounts.json.example accounts/accounts.json
# Or migrate: python scripts/migrate_cookies_to_registry.py
```

## Daily behaviour

| Activity | Default quota | Notes |
|----------|---------------|-------|
| Friend requests | 3-4 / day | >= 2000 friends or followers |
| Status posts | 3-5 / day | Topics from feed memory |
| Shares | 20 / day | Human-typed captions |
| Feed engagement | Continuous | Like, comment, share |

## Project layout

```
bot-agent/
  README.md
  docs/GUIDE_EN.md
  docs/GUIDE_BN.md
  scripts/run_agent_brain.py
  scripts/send_one_friend.py
  scripts/check_ollama.py
  scripts/unlock_browser_profile.py
  playwright_automation/
  profiles/
  cookies.txt
```

## Fleet (multiple bots)

```bash
python scripts/migrate_cookies_to_registry.py   # optional: cookies.txt → accounts.json
python scripts/fleet_launcher.py --max-bots 10 --phase 1
python scripts/fleet_launcher.py --status
```

See [docs/FLEET_SCALING.md](docs/FLEET_SCALING.md) for phase-wise scaling (5 → 50 → 500+ bots).

Docker fleet (one container per account, one-click start):

**Windows:** double-click `scripts/start_fleet_docker.bat`

```bat
scripts\start_fleet_docker.bat    REM start all bots
scripts\stop_fleet_docker.bat     REM stop all bots
```

Linux:

```bash
bash scripts/start_fleet_docker.sh
```

Sessions persist in `profiles/<account_id>/` — first run logs in, later runs reuse the saved session.

## Commands

```bash
python scripts/run_agent_brain.py --account-id YOUR_ID
python scripts/run_agent_brain.py --mode structured --headless --proxy http://user:pass@host:port
python scripts/run_agent_brain.py --skip-friends
python scripts/send_one_friend.py --account-id YOUR_ID
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--mode` | brain | Ollama decides vs fixed structured cycle (fleet default: structured) |
| `--proxy` | — | Per-bot proxy URL (`PROXY_URL` env also supported) |
| `--fleet-mode` | off | Worker mode: headless, no manual checkpoint wait |
| `--daily-friend-min/max` | 3 / 4 | Daily friend request range |
| `--daily-post-min/max` | 3 / 5 | Daily status post range |
| `--max-friend-send` | 1 | Friend requests per cycle |

## Documentation

- [English guide - system design](docs/GUIDE_EN.md)
- [Bangla guide - system design](docs/GUIDE_BN.md)

## Environment

Copy `.env.example` to `.env`. Key variables:

| Variable | Purpose |
|----------|---------|
| `OLLAMA_HOST` | Local Ollama server (default `127.0.0.1:11434`) |
| `OLLAMA_MODEL` | Model name (default `llama3.1:8b`) |
| `GEMINI_API_KEY` | Optional fallback when Ollama is offline |
| `MIN_AUDIENCE_FRIEND_REQUEST` | Min friends/followers for friend sends (default `2000`) |

Requirements: Python 3.10+, Ollama with `llama3.1:8b`, Chromium via Playwright.
