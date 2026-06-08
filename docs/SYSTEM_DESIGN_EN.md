# Facebook Agent — Whole Project System Design (English)

This document describes the **complete system design** of the `facebook-agent` project: purpose, architecture, components, data flows, persistence, AI integration, fleet scaling, and operations.

---

## Table of contents

1. [System overview](#1-system-overview)
2. [System context](#2-system-context)
3. [Architecture](#3-architecture)
4. [Technology stack — why and what](#4-technology-stack--why-and-what)
5. [Project structure](#5-project-structure)
6. [Core components](#6-core-components)
7. [Run modes and control flow](#7-run-modes-and-control-flow)
8. [Data flow diagrams](#8-data-flow-diagrams)
9. [Account and session management](#9-account-and-session-management)
10. [State persistence and daily quotas](#10-state-persistence-and-daily-quotas)
11. [AI and LLM integration](#11-ai-and-llm-integration)
12. [Browser automation and stealth](#12-browser-automation-and-stealth)
13. [Facebook activity flows](#13-facebook-activity-flows)
14. [Fleet and Docker deployment](#14-fleet-and-docker-deployment)
15. [External integrations](#15-external-integrations)
16. [Configuration reference](#16-configuration-reference)
17. [CLI entry points](#17-cli-entry-points)
18. [Security and operational notes](#18-security-and-operational-notes)
19. [Troubleshooting](#19-troubleshooting)

---

## 1. System overview

### 1.1 Purpose

`facebook-agent` is a **CLI-driven Python automation system** that controls Chromium via Playwright to perform **human-like Facebook activity** for a single account (or many accounts in fleet mode).

**Capabilities:**

| Capability | Description |
|------------|-------------|
| Feed engagement | Scroll, like, react, comment, and share posts |
| Status posts | Publish original posts based on trending topics from feed memory |
| Friend requests | Send a small daily number of requests to high-audience profiles (≥ 2,000 friends/followers) |
| Session persistence | Save browser cookies and daily counters across restarts |
| Multi-account fleet | Run many isolated bots via subprocess launcher or Docker |

### 1.2 What this system is NOT

- **Not a web server** — there is no REST API, FastAPI, or Flask backend exposed by this project.
- **Not a database-backed app** — all state is stored as JSON files under `profiles/`.
- **Not a Facebook API client** — all actions go through the browser UI like a real user.

### 1.3 Primary entry point

```bash
python scripts/run_agent_brain.py
```

---

## 2. System context

```mermaid
flowchart LR
    subgraph Operator
        U[User / DevOps]
    end

    subgraph facebook_agent["facebook-agent (this project)"]
        CLI[CLI scripts]
        PA[playwright_automation]
        CLI --> PA
    end

    subgraph External
        OLL[Ollama LLM]
        GEM[Gemini API]
        FB[Facebook.com]
        PX[HTTP Proxy]
        WH[Alert Webhook]
    end

    U -->|runs commands| CLI
    PA -->|HTTP /api/chat| OLL
    PA -->|HTTPS fallback| GEM
    PA -->|Playwright browser| FB
    PA -->|optional| PX
    PA -->|optional alerts| WH
```

---

## 3. Architecture

### 3.1 Layered architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Presentation / Entry Layer                                 │
│  scripts/run_agent_brain.py, fleet_launcher.py, docker_*    │
└────────────────────────────┬────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────┐
│  Orchestration Layer                                        │
│  agent_executor.py — cycles, quotas, brain/structured modes │
└──────────────┬──────────────────────────┬───────────────────┘
               │                          │
┌──────────────▼──────────┐   ┌───────────▼───────────────────┐
│  Decision / AI Layer    │   │  Browser Automation Layer     │
│  agent_brain.py         │   │  actions.py, bot_core.py        │
│  brain.py, ai_comment.py│   │  stealth_config, human_behavior │
└──────────────┬──────────┘   └───────────┬───────────────────┘
               │                          │
┌──────────────▼──────────────────────────▼───────────────────┐
│  Domain Layer (Facebook-specific)                             │
│  facebook_graph, facebook_login, post_engagement,             │
│  profile_engagement, account_session, account_registry        │
└────────────────────────────┬────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────┐
│  Infrastructure Layer                                       │
│  profiles/ JSON files, accounts/ credentials, .env config   │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Component interaction (single bot)

```
                    ┌─────────────────────────┐
                    │  scripts/run_agent_brain │
                    │  (CLI, login, loop)      │
                    └────────────┬────────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         ▼                       ▼                       ▼
  account_registry         agent_executor           BaseBot
  account_session          (cycles, quotas)        (Playwright)
  facebook_login                 │                       │
         │           ┌──────────┴──────────┐            │
         │           ▼                     ▼            │
         │      agent_brain            actions ◄─────────┘
         │      (JSON decisions)    (UI automation)
         │           │
         │           ▼
         │      brain.py ──HTTP──► Ollama (local)
         │           │
         │           ▼
         └──────► ai_comment.py ──HTTP──► Gemini (optional)
                                   │
                                   ▼
                            Facebook (browser)
```

### 3.3 Layer responsibilities

| Layer | Module(s) | Role |
|-------|-----------|------|
| Entry | `scripts/run_agent_brain.py` | Parse CLI args, start browser, infinite cycle loop |
| Entry | `scripts/fleet_launcher.py` | Spawn N subprocess bots with stagger and auto-restart |
| Entry | `scripts/docker_entrypoint.py` | Container entry: stagger delay → run agent |
| Session | `account_registry.py` | Load credentials from JSON, `.env`, or `cookies.txt` |
| Session | `account_session.py` | Parse cookies, detect login state, checkpoint wait |
| Session | `facebook_login.py` | Email/password login, checkpoint URL detection |
| Orchestration | `agent_executor.py` | Daily quotas, brain steps, structured cycles |
| Decision | `agent_brain.py`, `brain.py` | Ask Ollama for next JSON action |
| Content | `ai_comment.py` | Comments, share captions, status posts |
| Browser | `actions.py`, `bot_core.py` | Playwright clicks, scroll, type, react |
| Social graph | `facebook_graph.py` | Friend suggestions, audience checks |
| Feed | `post_engagement.py` | Pick visible posts, dedup fingerprints |
| Profile | `profile_engagement.py` | Profile stalk browsing before friend sends |
| Stealth | `stealth_config.py`, `human_behavior.py` | Fingerprint masking, natural typing |
| Fleet ops | `fleet_status.py` | Health JSON + optional webhook alerts |
| Profile lock | `browser_profile.py` | Release Chromium lock files |

---

## 4. Technology stack — why and what

This section explains **each technology used in the project**, **what it does**, and **why it was chosen** instead of alternatives.

### 4.1 Summary table

| Technology | What it does in this project | Why we use it |
|------------|------------------------------|---------------|
| **Python ≥ 3.10** | Main language for all scripts and the `playwright_automation` library | Rich async support, fast prototyping, large ecosystem for automation and HTTP; team-friendly for CLI tools |
| **Playwright (Chromium)** | Drives a real browser: click, scroll, type, read DOM, save cookies | Facebook has no public API for feed/comment/share; UI automation behaves like a real user session |
| **playwright-stealth** | Patches automation fingerprints (`navigator.webdriver`, etc.) | Reduces bot-detection signals that plain Playwright exposes |
| **Custom stealth scripts** | Adds canvas/WebGL/WebRTC noise and fingerprint init scripts | Extra layer beyond the library; tuned for mobile Facebook |
| **httpx** | Async HTTP calls to Ollama, Gemini, and fleet alert webhooks | Modern async client; simpler than `requests` inside Playwright’s async loop |
| **python-dotenv** | Loads `.env` into environment variables at startup | Keeps secrets and tuning out of source code; one file per machine |
| **tzdata** | Supplies IANA timezone data (e.g. `Asia/Dhaka`) for the browser | Windows often lacks full zoneinfo; matches user locale in viewport |
| **truststore + certifi** | Fixes TLS/SSL when calling Gemini behind corporate proxy or on Windows | Avoids certificate errors without disabling SSL verification |
| **Ollama + llama3.1:8b** | Local LLM: next-action JSON, comments, captions, status posts | Runs offline, no per-token cloud cost, low latency on a desktop GPU/CPU |
| **Google Gemini** | Fallback text generation when Ollama fails or is overloaded | Keeps comments/captions working in fleet mode without a second local GPU |
| **Docker + Compose** | One container per bot, isolated env, staggered startup | Scale many accounts on servers; reproducible runtime with Playwright base image |
| **JSON files (`profiles/`)** | Stores sessions, daily quotas, fleet health | No DB setup; easy to inspect, back up, and mount in Docker volumes |
| **asyncio** | Runs Playwright and HTTP concurrently in one process | Playwright Python API is async-native; one bot = one event loop |
| **setuptools / pyproject.toml** | Packages `playwright_automation` as installable module | Clean imports from `scripts/` without fragile path hacks |

### 4.2 Technology details

#### Python 3.10+

- **Role:** Entry scripts (`scripts/`), core library (`playwright_automation/`), fleet launcher, Docker entrypoint.
- **Why not Node.js?** Playwright exists in both, but Python fits better for long-running automation loops, JSON quota files, and local LLM HTTP clients in one codebase.
- **Why not Java/C#?** Slower iteration for DOM-heuristic tweaks and prompt changes.

#### Playwright + Chromium

- **Role:** `bot_core.py` launches persistent Chromium context; `actions.py` performs all Facebook UI steps.
- **What it handles:** Navigation, selectors, mobile viewport, proxy, `storage_state.json` save/load.
- **Why Playwright over Selenium?** Better auto-wait, persistent contexts, modern API, official stealth integration path.
- **Why Chromium?** Facebook is tested heavily on Chrome/Chromium; mobile UA still uses Chromium engine.

#### playwright-stealth + custom scripts

- **Role:** `stealth_config.py` applies library patches and injects init scripts before page load.
- **Why both?** Library covers common leaks; custom scripts add project-specific fingerprint variation for fleet bots.

#### httpx

- **Role:** `brain.py` → Ollama `/api/chat`; `ai_comment.py` → Gemini REST; `fleet_status.py` → webhook POST.
- **Why not `urllib`?** Async support and cleaner timeout/error handling for fleet throttling retries.

#### python-dotenv

- **Role:** Loaded in `run_agent_brain.py` and fleet scripts from project-root `.env`.
- **Why not hard-coded config?** Different machines (dev PC, Docker host, fleet server) need different Ollama hosts, proxies, and keys without code changes.

#### tzdata

- **Role:** Browser timezone set via CLI `--timezone` (default `Asia/Dhaka`).
- **Why:** Mismatch between system TZ and Facebook session TZ is a subtle detection signal.

#### truststore + certifi

- **Role:** SSL context for Gemini HTTPS on Windows or MITM corporate proxies.
- **Why:** Default Python cert store on Windows sometimes fails where browser TLS succeeds.

#### Ollama (llama3.1:8b)

- **Role:** Primary “brain” — structured JSON decisions, Bengali/English comments, trending status posts.
- **Why local LLM?** Privacy (prompts stay on machine), zero API billing, works when internet is slow except Facebook.
- **Why llama3.1:8b?** Good balance of speed and quality on 8–16 GB RAM; small enough for many fleet workers sharing one GPU server.
- **Why not OpenAI only?** Cost at scale (500+ bots) and dependency on external uptime.

#### Google Gemini (optional)

- **Role:** Fallback in `ai_comment.py` when Ollama returns errors or times out.
- **Why optional?** Fleet structured mode can run with minimal LLM; Gemini only needed for comment/caption resilience.

#### Docker + Docker Compose

- **Role:** `Dockerfile` (Playwright Python image), `docker-compose.yml`, generated fleet compose files.
- **What it provides:** Per-account `ACCOUNT_ID`, `FLEET_MODE=1`, headless structured agent, volume mount for `profiles/`.
- **Why not Kubernetes only?** Compose is enough for Phase 1–2; K8s mentioned in FLEET_SCALING for Phase 3.

#### JSON file persistence (no database)

- **Role:** `profiles/<account_id>/` — quotas, cookies, fleet status.
- **Why not PostgreSQL/SQLite?** Single-writer per account, no joins, no migrations; ops can `cat` files for debugging.
- **Trade-off:** Not ideal for centralized analytics; fleet `--status` aggregates JSON files instead.

#### asyncio

- **Role:** `run_agent_brain.py` uses `asyncio.run()`; all page actions and Ollama calls interleave without blocking threads.
- **Why not threaded Selenium?** Harder to reason about; Playwright Python is designed for async.

### 4.3 Dependencies (`requirements.txt`)

| Package | Version | Used for |
|---------|---------|----------|
| `playwright` | ≥ 1.49.0 | Browser automation |
| `playwright-stealth` | ≥ 2.0.0 | Anti-detection patches |
| `httpx` | ≥ 0.27.0 | Ollama, Gemini, webhooks |
| `tzdata` | ≥ 2024.1 | Browser timezone data |
| `python-dotenv` | ≥ 1.0.0 | `.env` loading |
| `truststore` | ≥ 0.10.0 | OS trust store for TLS |
| `certifi` | ≥ 2024.7.4 | CA bundle fallback |

### 4.4 What we deliberately did not use

| Not used | Reason |
|----------|--------|
| Facebook Graph API | No access to full feed engagement, friend suggestions UI, or composer like a logged-in user |
| FastAPI / Flask | No HTTP server needed; CLI + subprocess fleet is enough |
| PostgreSQL / Redis | File JSON is sufficient per-bot state; avoids ops overhead |
| Selenium | Playwright offers better persistence and stealth story |
| Cloud-only LLM | Too expensive and latency-sensitive for continuous brain mode |

---

## 5. Project structure

```
bot-agent/
├── README.md
├── pyproject.toml                 # Package: facebook-agent 0.2.0
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
│
├── docs/
│   ├── SYSTEM_DESIGN_EN.md        # This document
│   ├── SYSTEM_DESIGN_BN.md        # Bangla version
│   └── FLEET_SCALING.md
│
├── playwright_automation/         # Core library
│   ├── account_registry.py
│   ├── account_session.py
│   ├── actions.py
│   ├── agent_brain.py
│   ├── agent_executor.py
│   ├── ai_comment.py
│   ├── bot_core.py
│   ├── brain.py
│   ├── browser_profile.py
│   ├── facebook_graph.py
│   ├── facebook_login.py
│   ├── fleet_status.py
│   ├── human_behavior.py
│   ├── post_engagement.py
│   ├── profile_engagement.py
│   ├── stealth_config.py
│   └── user_agent_rotation.py
│
├── scripts/                       # CLI entry points
│   ├── run_agent_brain.py         # ★ Main agent runner
│   ├── send_one_friend.py
│   ├── fleet_launcher.py
│   ├── check_ollama.py
│   ├── migrate_cookies_to_registry.py
│   ├── unlock_browser_profile.py
│   ├── docker_entrypoint.py
│   ├── docker_fleet_compose.py
│   ├── generate_compose_services.py
│   └── setup.bat / start_fleet_docker.*
│
├── accounts/                      # Credentials (gitignored at runtime)
│   ├── accounts.json.example
│   └── account.env.example
│
└── profiles/                      # Runtime state (gitignored)
    └── <account_id>/
        ├── storage_state.json
        ├── daily_friend_quota.json
        ├── daily_post_quota.json
        ├── daily_share_quota.json
        ├── fleet_status.json
        └── browser/               # Chromium user data dir
```

---

## 6. Core components

### 6.1 `BaseBot` (`bot_core.py`)

- Creates a **persistent Chromium context** per account.
- Applies proxy, user-agent rotation, stealth scripts, and timezone.
- Loads/saves `storage_state.json` for cookie persistence.

### 6.2 `AgentSession` (`agent_executor.py`)

- Holds runtime state: feed memory snippets, quota counters, cycle metadata.
- Executes `agent_step()` for each Ollama decision.
- Runs structured cycles and daily phases (friend, post, share).

### 6.3 `AgentDecision` (`agent_brain.py`)

Ollama returns strict JSON describing the next action:

```json
{
  "action": "comment_post",
  "location": "newsfeed",
  "thought_process": "This post discusses local politics..."
}
```

Supported actions include scroll, like, comment, share, navigate tabs, and idle observe.

### 6.4 `Brain` (`brain.py`)

- HTTP client for Ollama `/api/chat` and `/api/tags`.
- Fleet throttling via `FLEET_OLLAMA_MIN_INTERVAL_SEC` to avoid overloading shared Ollama.

### 6.5 `ai_comment.py`

- Generates comments, share captions, and status posts.
- Primary: Ollama. Fallback: Gemini API.
- Supports Bengali/English language detection via `STATUS_BN_RATIO`.

---

## 7. Run modes and control flow

### 7.1 Brain mode (default)

Each **cycle**:

1. **Friend phase** (if daily quota remains): browse 15–25 suggestion rows, stalk profiles 12–28s, send request if audience ≥ 2k. Max **1 send per cycle**, **3–4 per day**.
2. **Status post phase** (if quota remains): scroll feed, collect snippets, infer trending topics, publish one post.
3. **Ollama steps** (`--steps-per-burst`, default 6–8): observe page → JSON action → Playwright execute.
4. **Share top-up** if daily share quota (20/day) not met.
5. Pause 8–18s (or 45–120s depending on config), save state, repeat.

If Ollama is offline, the executor falls back to deterministic offline actions (scroll + basic engagement).

### 7.2 Structured mode (`--mode structured`)

Fixed pipeline each cycle — lower LLM load, used by fleet launcher and Docker:

1. Friend send + accept (optional)
2. Feed rounds: scroll → like → comment → share
3. Status post

### 7.3 Brain cycle flowchart

```mermaid
flowchart TD
    A[Start cycle] --> B{Friend quota left?}
    B -->|yes| C[Random stalk suggestions]
    B -->|no| D
    C --> D{Post quota left?}
    D -->|yes| E[Scroll feed + trending post]
    D -->|no| F
    E --> F[Ollama steps x N]
    F --> G{Share quota left?}
    G -->|yes| H[Share to timeline]
    G -->|no| I
    H --> I[Save state + pause]
    I --> A
```

---

## 8. Data flow diagrams

### 8.1 Single action step (brain mode)

```mermaid
sequenceDiagram
    participant R as run_agent_brain
    participant E as agent_executor
    participant B as agent_brain
    participant O as Ollama
    participant A as actions
    participant F as Facebook

    R->>E: agent_step(session, page)
    E->>B: decide_next_action(state)
    B->>O: POST /api/chat
    O-->>B: JSON AgentDecision
    B-->>E: AgentDecision
    E->>A: execute action on page
    A->>F: Playwright UI interaction
    F-->>A: DOM updated
    A-->>E: result
    E-->>R: step complete
```

### 8.2 Comment generation flow

```mermaid
sequenceDiagram
    participant E as agent_executor
    participant P as post_engagement
    participant AI as ai_comment
    participant O as Ollama
    participant G as Gemini
    participant A as actions

    E->>P: pick_fresh_visible_post()
    P-->>E: post snippet
    E->>AI: generate_comment_for_post()
    AI->>O: generate text
    alt Ollama fails
        AI->>G: fallback generateContent
    end
    AI-->>E: comment text
    E->>A: comment_on_post(human typing)
```

---

## 9. Account and session management

### 9.1 Account sources (priority order)

1. **`accounts/accounts.json`** (recommended) — JSON array with `id`, `password`, `cookies`, `proxy`
2. **`accounts/<account_id>.env`** — per-account env file
3. **Legacy `cookies.txt`** — three lines per account

### 9.2 Legacy `cookies.txt` format

```
account_id
password
c_user=...; xs=...; datr=...
```

Migrate to registry:

```bash
python scripts/migrate_cookies_to_registry.py
```

### 9.3 Login flow

1. Load saved `storage_state.json` if exists
2. Navigate to Facebook — check if already logged in
3. If not logged in: use cookies seed or `stealthy_facebook_login()`
4. If checkpoint detected: wait up to 30 min for manual completion (skipped in `--fleet-mode`)

### 9.4 Proxy

Per-account proxy via `accounts.json`, `PROXY_URL` env, or `--proxy` CLI flag:

```
http://user:pass@host:port
```

---

## 10. State persistence and daily quotas

All state lives under `profiles/<account_id>/`:

| File | Purpose |
|------|---------|
| `storage_state.json` | Chromium cookies + localStorage |
| `daily_friend_quota.json` | Friends sent today / daily target (3–4) |
| `daily_post_quota.json` | Status posts today / target (3–5) |
| `daily_share_quota.json` | Shares today / fingerprints (target 20) |
| `fleet_status.json` | Bot state, PID, quotas, errors, checkpoint flag |
| `browser/` | Chromium persistent user data directory |

### Daily activity quotas (defaults)

| Activity | Default quota | Notes |
|----------|---------------|-------|
| Friend requests | 3–4 / day | Only profiles with ≥ 2,000 friends/followers |
| Status posts | 3–5 / day | Topics from feed memory |
| Shares | 20 / day | Human-typed captions to own timeline |
| Feed engagement | Continuous | Like, comment, share during cycles |

Quotas reset daily based on the calendar date stored in each JSON file.

---

## 11. AI and LLM integration

### 11.1 Task routing

| Task | Primary | Fallback |
|------|---------|----------|
| Next action decision | Ollama | Offline scroll/like |
| Comments | Ollama | Gemini API |
| Share captions | Ollama | Gemini API |
| Status posts | Ollama | Skip |
| Profile audience count | DOM parsing | Ollama text read |

### 11.2 Ollama configuration

| Variable | Default |
|----------|---------|
| `OLLAMA_HOST` | `127.0.0.1:11434` |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `llama3.1:8b` |

Health check:

```bash
python scripts/check_ollama.py
```

### 11.3 Gemini fallback

Used when Ollama fails for comment/caption generation. Set `GEMINI_API_KEY` in `.env`.

### 11.4 Fleet LLM throttling

When many bots share one Ollama instance, `FLEET_OLLAMA_MIN_INTERVAL_SEC` (default 8s) enforces minimum gap between API calls across workers.

---

## 12. Browser automation and stealth

| Feature | Implementation |
|---------|----------------|
| Persistent profile | Per-account Chromium user data dir |
| Viewport | Mobile 360×800 (default) or desktop |
| User agent | Random rotation via `user_agent_rotation.py` |
| Stealth | `playwright-stealth` + canvas/WebGL/WebRTC noise |
| Mouse movement | Bezier curves |
| Scrolling | Segment-based human scroll |
| Typing | Typos, backspace, word pauses via `human_behavior.py` |

Human typing env vars: `HUMAN_TYPO_RATE`, `HUMAN_RETHINK_RATE`, `HUMAN_WORD_PAUSE_MIN`.

---

## 13. Facebook activity flows

### 13.1 Friend request flow

`facebook_graph.py` → `run_daily_friend_send_phase()`:

1. Open friend suggestions page
2. Light scroll (5 passes)
3. Click random suggestion row (mobile-safe selectors)
4. Browse profile 12–28 seconds (`profile_engagement.py`)
5. Read friends/followers count (DOM + Ollama if needed)
6. Send request if count ≥ `MIN_AUDIENCE_FRIEND_REQUEST` (default 2000)
7. Stop at daily cap

Utility: `python scripts/send_one_friend.py`

### 13.2 Status post flow

1. Scroll feed → store snippets in `feed_memory_snippets`
2. Infer trending topics via Ollama
3. Generate original opinion post (not generic filler)
4. Publish via composer with human typing

Skipped if fewer than 2 feed snippets collected.

### 13.3 Share flow

1. Pick visible feed post (skip stories/reels)
2. Generate caption (Ollama / Gemini)
3. Human natural typing: delays, typos, backspace
4. Confirm share, return to feed

---

## 14. Fleet and Docker deployment

### 14.1 Host-based fleet (`fleet_launcher.py`)

- Spawns one subprocess per account from `accounts.json`
- Staggered startup (30–120s between bots)
- Auto-restart on crash
- `--status` reads all `profiles/*/fleet_status.json`

Phase limits:

| Phase | Max bots | Use case |
|-------|----------|----------|
| 1 | 10 | Verify on single PC |
| 2 | 50 | Single server |
| 3 | 600 | Distributed |

```bash
python scripts/fleet_launcher.py --max-bots 10 --phase 1
python scripts/fleet_launcher.py --status
```

### 14.2 Docker fleet

Each container runs one bot with:

- `FLEET_MODE=1` (headless, no manual checkpoint wait)
- `--mode structured` (default in Docker)
- Staggered startup via `FLEET_STAGGER_MIN/MAX`
- Sessions mounted from host `profiles/`

```bash
docker compose up --build bot1 bot2 bot3
python scripts/generate_compose_services.py --count 10
```

See [FLEET_SCALING.md](FLEET_SCALING.md) for RAM/CPU estimates and scaling phases.

### 14.3 Fleet monitoring

`fleet_status.py` writes health JSON per bot. Optional `FLEET_ALERT_WEBHOOK` POSTs alerts on checkpoint or crash.

---

## 15. External integrations

| Service | Protocol | Usage |
|---------|----------|-------|
| **Ollama** | HTTP `POST /api/chat` | Primary LLM for decisions and content |
| **Google Gemini** | HTTPS `generativelanguage.googleapis.com` | Fallback for comments/captions |
| **Facebook** | Browser (Playwright) | All social actions via UI |
| **HTTP Proxy** | Playwright proxy config | Per-account IP isolation |
| **Webhook** | HTTP POST JSON | Optional fleet alerts |

---

## 16. Configuration reference

Copy `.env.example` → `.env`.

### AI / LLM

| Variable | Default | Purpose |
|----------|---------|---------|
| `OLLAMA_HOST` | `127.0.0.1:11434` | Ollama host:port |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Full base URL (overrides host) |
| `OLLAMA_MODEL` | `llama3.1:8b` | Model for all Ollama calls |
| `GEMINI_API_KEY` | (empty) | Gemini fallback key |

### Facebook behaviour

| Variable | Default | Purpose |
|----------|---------|---------|
| `MIN_AUDIENCE_FRIEND_REQUEST` | `2000` | Min friends/followers for friend send |
| `HUMAN_TYPO_RATE` | `0.045` | Typo probability |
| `HUMAN_RETHINK_RATE` | `0.10` | Backspace/rethink probability |
| `STATUS_BN_RATIO` | `0.65` | Ratio of Bengali vs English status posts |

### Fleet / Docker

| Variable | Default | Purpose |
|----------|---------|---------|
| `FLEET_MODE` | `0` | Headless fleet worker mode |
| `FLEET_OLLAMA_MIN_INTERVAL_SEC` | `8.0` | Min gap between Ollama calls |
| `FLEET_ALERT_WEBHOOK` | (empty) | Alert webhook URL |
| `FLEET_STAGGER_MIN/MAX` | `15` / `90` | Docker startup stagger (seconds) |
| `FLEET_AGENT_MODE` | `structured` | Agent mode inside Docker |
| `ACCOUNT_ID` | — | Account ID for container |
| `PROXY_URL` | — | Proxy override |

---

## 17. CLI entry points

| Command | Purpose |
|---------|---------|
| `python scripts/run_agent_brain.py` | **Main agent** — single account |
| `python scripts/send_one_friend.py` | Friend requests only |
| `python scripts/fleet_launcher.py` | Launch N bot subprocesses |
| `python scripts/fleet_launcher.py --status` | Read fleet health |
| `python scripts/check_ollama.py` | Verify Ollama reachability |
| `python scripts/migrate_cookies_to_registry.py` | Migrate legacy cookies |
| `python scripts/unlock_browser_profile.py --kill-chrome` | Fix profile lock |
| `python scripts/generate_compose_services.py --count 10` | Generate Docker services |

### Key CLI flags (`run_agent_brain.py`)

| Flag | Default | Purpose |
|------|---------|---------|
| `--mode` | `brain` | `brain` or `structured` |
| `--account-id` | env | Facebook account ID |
| `--proxy` | env | HTTP proxy URL |
| `--fleet-mode` | off | Worker mode (headless, no checkpoint wait) |
| `--headless` | off | Headless Chromium |
| `--mobile` / `--no-mobile` | mobile on | Mobile vs desktop viewport |
| `--timezone` | `Asia/Dhaka` | Browser timezone |
| `--daily-friend-min/max` | 3 / 4 | Friend request range |
| `--daily-post-min/max` | 3 / 5 | Status post range |
| `--min-daily-shares` | 20 | Share target |
| `--steps-per-burst` | 6 | Ollama steps per brain cycle |
| `--skip-friends` | off | Skip friend activity |

---

## 18. Security and operational notes

- **Credentials** — Never commit `.env`, `accounts/accounts.json`, or `cookies.txt`. They are gitignored.
- **Proxies** — Use one residential/mobile proxy per account in fleet mode to reduce correlation.
- **Checkpoints** — Facebook may require manual verification; the agent waits in non-fleet mode.
- **Rate limits** — Daily quotas are intentionally conservative; adjust via CLI flags with care.
- **Terms of Service** — Automated activity may violate Facebook ToS; users assume full responsibility.

---

## 19. Troubleshooting

| Problem | Fix |
|---------|-----|
| Ollama not reachable | Start Ollama; set `OLLAMA_HOST=127.0.0.1:11434` |
| Profile locked | `python scripts/unlock_browser_profile.py --kill-chrome` |
| 0 friend sends | Check Ollama; verify suggestions page loads |
| No status posts | Run more cycles to build feed memory (need ≥ 2 snippets) |
| Checkpoint | Complete manually in browser; agent waits up to 30 min |
| Gemini SSL errors on Windows | `truststore` and `certifi` are included in requirements |
| Fleet bots crash loop | Check `profiles/<id>/fleet_status.json` for error details |

---

*Document version: matches `facebook-agent` 0.2.0*
