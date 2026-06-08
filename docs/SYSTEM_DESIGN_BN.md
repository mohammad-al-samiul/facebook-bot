# Facebook Agent — পুরো প্রজেক্ট সিস্টেম ডিজাইন (বাংলা)

এই ফাইলে `facebook-agent` প্রজেক্টটা ভেতর থেকে কীভাবে চলে — সেটা খুলে বলা আছে। আর্কিটেকচার, ফাইল স্ট্রাকচার, daily quota, AI, fleet — সব এক জায়গায়।

---

## সূচিপত্র

1. [প্রজেক্টটা আসলে কী?](#১-প্রজেক্টটা-আসলে-কী)
2. [বাইরের দুনিয়ার সাথে কীভাবে জড়িত](#২-বাইরের-দুনিয়ার-সাথে-কীভাবে-জড়িত)
3. [আর্কিটেকচার](#৩-আর্কিটেকচার)
4. [কোন technology কেন নিয়েছি](#৪-কোন-technology-কেন-নিয়েছি)
5. [ফোল্ডার ও ফাইল](#৫-ফোল্ডার-ও-ফাইল)
6. [মূল মডিউলগুলো](#৬-মূল-মডিউলগুলো)
7. [চালানোর দুটো মোড](#৭-চালানোর-দুটো-মোড)
8. [ডেটা কীভাবে ঘুরে](#৮-ডেটা-কীভাবে-ঘুরে)
9. [অ্যাকাউন্ট ও লগইন](#৯-অ্যাকাউন্ট-ও-লগইন)
10. [কোথায় কী save হয় + daily limit](#১০-কোথায়-কী-save-হয়--daily-limit)
11. [AI / LLM কীভাবে কাজে লাগে](#১১-ai--llm-কীভাবে-কাজে-লাগে)
12. [ব্রাউজার automation ও bot ধরা পড়া এড়ানো](#১২-ব্রাউজার-automation-ও-bot-ধরা-পড়া-এড়ানো)
13. [Facebook-এ কী কী করে](#১৩-facebook-এ-কী-কী-করে)
14. [অনেক বট একসাথে (fleet + Docker)](#১৪-অনেক-বট-একসাথে-fleet--docker)
15. [বাইরের service গুলো](#১৫-বাইরের-service-গুলো)
16. [`.env` ও config](#১৬-env-ও-config)
17. [কোন command কী করে](#১৭-কোন-command-কী-করে)
18. [নিরাপত্তা — যা মাথায় রাখবেন](#১৮-নিরাপত্তা--যা-মাথায়-রাখবেন)
19. [সমস্যা হলে কী করবেন](#১৯-সমস্যা-হলে-কী-করবেন)

---

## ১. প্রজেক্টটা আসলে কী?

### ১.১ এক কথায়

`facebook-agent` হলো একটা **Python CLI tool**। Playwright দিয়ে Chromium ব্রাউজার চালায়, আর **local Ollama** (তোমার PC-তে চলা LLM) দিয়ে ঠিক করে নেয় পরের কাজ কী — লাইক নাকি কমেন্ট, স্ক্রল নাকি পোস্ট। একবারে একটা Facebook account; চাইলে fleet mode-এ অনেকগুলো একসাথে।

### ১.২ কী কী করতে পারে

| কাজ | সংক্ষেপে |
|-----|---------|
| ফিড | স্ক্রল, লাইক, কমেন্ট, শেয়ার |
| স্ট্যাটাস পোস্ট | ফিডে যা দেখছে সেখান থেকে topic বের করে নিজের মতো পোস্ট |
| ফ্রেন্ড রিকোয়েস্ট | দিনে ৩–৪টা; শুধু যাদের ২০০০+ ফ্রেন্ড/ফলোয়ার |
| সেশন save | বন্ধ করে আবার চালালেও লগইন থাকে, daily count-ও থাকে |
| fleet | এক PC বা server-এ অনেক account — subprocess বা Docker |

### ১.৩ যেটা **না**

- **ওয়েব অ্যাপ না** — কোনো REST API server নেই।
- **ডাটাবেস নেই** — সব `profiles/` ফোল্ডারে JSON ফাইল।
- **Facebook Graph API ব্যবহার করে না** — যা করবে সব real browser দিয়ে, যেন তুমি নিজে বসে আছ।

### ১.৪ শুরু করার command

```bash
python scripts/run_agent_brain.py
```

---

## ২. বাইরের দুনিয়ার সাথে কীভাবে জড়িত

```mermaid
flowchart LR
    subgraph Operator
        U[তুমি / DevOps]
    end

    subgraph facebook_agent["facebook-agent"]
        CLI[CLI scripts]
        PA[playwright_automation]
        CLI --> PA
    end

    subgraph External
        OLL[local Ollama]
        GEM[Gemini API]
        FB[Facebook.com]
        PX[Proxy]
        WH[Webhook]
    end

    U -->|command দাও| CLI
    PA -->|HTTP| OLL
    PA -->|backup| GEM
    PA -->|browser| FB
    PA -->|optional| PX
    PA -->|alert| WH
```

---

## ৩. আর্কিটেকচার

### ৩.১ স্তরগুলো (উপর থেকে নিচে)

```
┌─────────────────────────────────────────────────────────────┐
│  Entry — তুমি command দাও                                  │
│  run_agent_brain.py, fleet_launcher.py, docker_*           │
└────────────────────────────┬────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────┐
│  Orchestration — cycle, quota, brain/structured mode       │
│  agent_executor.py                                          │
└──────────────┬──────────────────────────┬───────────────────┘
               │                          │
┌──────────────▼──────────┐   ┌───────────▼───────────────────┐
│  AI — সিদ্ধান্ত + text   │   │  Browser — click, type, scroll│
│  agent_brain, ai_comment  │   │  actions, bot_core, stealth   │
└──────────────┬──────────┘   └───────────┬───────────────────┘
               │                          │
┌──────────────▼──────────────────────────▼───────────────────┐
│  Facebook logic — login, friend, feed, profile               │
└────────────────────────────┬────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────┐
│  Storage — profiles/, accounts/, .env                       │
└─────────────────────────────────────────────────────────────┘
```

### ৩.২ একটা বট চললে কী কী মিলে কাজ করে

```
                    ┌─────────────────────────┐
                    │  run_agent_brain.py     │
                    │  (লগইন + loop)          │
                    └────────────┬────────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         ▼                       ▼                       ▼
  account_registry         agent_executor           BaseBot
  (login, cookie)          (cycle, quota)          (Playwright)
         │                       │                       │
         │           ┌───────────┴───────────┐           │
         │           ▼                       ▼           │
         │      agent_brain              actions ◄─────────┘
         │      (Ollama থেকে JSON)    (UI তে click/type)
         │           │
         │           ▼
         │      brain.py ──────► local Ollama
         │           │
         │           ▼
         └──────► ai_comment.py ──► Gemini (backup, optional)
                                   │
                                   ▼
                            Facebook (browser)
```

### ৩.৩ কোন ফাইল কী করে — দ্রুত তালিকা

| অংশ | ফাইল | কাজ |
|-----|------|-----|
| শুরু | `run_agent_brain.py` | argument নেয়, browser খোলে, loop চালায় |
| fleet | `fleet_launcher.py` | account প্রতি একটা process, crash হলে restart |
| Docker | `docker_entrypoint.py` | container-এ agent start |
| account | `account_registry.py` | `accounts.json` / `.env` / `cookies.txt` পড়ে |
| login | `account_session.py`, `facebook_login.py` | cookie, login check, checkpoint |
| মাথা | `agent_executor.py` | daily quota, brain step, structured cycle |
| AI | `agent_brain.py`, `brain.py` | local Ollama-কে জিজ্ঞেস — পরের action কী |
| text | `ai_comment.py` | comment, caption, status post লেখে |
| browser | `actions.py`, `bot_core.py` | click, scroll, type |
| friend | `facebook_graph.py` | suggestion, follower count, add friend |
| feed | `post_engagement.py` | কোন post-এ কাজ করবে |
| profile | `profile_engagement.py` | friend পাঠানোর আগে profile ঘুরে দেখা |
| stealth | `stealth_config.py`, `human_behavior.py` | bot detect এড়ানো, মানুষের মতো typing |
| fleet status | `fleet_status.py` | health JSON + webhook alert |
| lock fix | `browser_profile.py` | Chromium profile lock খুলে দেয় |

---

## ৪. কোন technology কেন নিয়েছি

নিচে প্রতিটা tool **কী কাজে লাগে** আর **অন্য কিছু না নিয়ে এটা কেন** — সেটা বলা আছে।

### ৪.১ এক নজরে

| Technology | এখানে কী করে | কেন এটা |
|------------|-------------|---------|
| **Python 3.10+** | সব script + library | automation, async, দ্রুত change — dev-friendly |
| **Playwright + Chromium** | real browser চালায় | Facebook-এ feed/comment/share-এর public API নেই |
| **playwright-stealth** | `webdriver` ইত্যাদি লুকায় | সোজা Playwright-এ bot signal বেশি |
| **Custom stealth scripts** | canvas/WebGL noise | mobile Facebook-এ extra সুরক্ষা |
| **httpx** | Ollama, Gemini, webhook call | async loop-এ `requests`-এর চেয়ে সুবিধা |
| **python-dotenv** | `.env` থেকে config load | password/code-এ না রেখে আলাদা ফাইল |
| **tzdata** | browser timezone (`Asia/Dhaka`) | Windows-এ timezone data কম থাকে |
| **truststore + certifi** | Gemini HTTPS ঠিক রাখে | office proxy / Windows-এ SSL error কমায় |
| **local Ollama + llama3.1:8b** | সিদ্ধান্ত + comment + post | PC-তে চলে, cloud bill নেই, দ্রুত |
| **Google Gemini** | Ollama fail হলে backup text | comment/caption বন্ধ হবে না |
| **Docker + Compose** | account প্রতি container | server-এ scale করা সহজ |
| **JSON (`profiles/`)** | session, quota, status save | DB লাগে না, ফাইল খুলে দেখা যায় |
| **asyncio** | browser + HTTP একসাথে | Playwright Python async-ই |
| **pyproject.toml** | package install | import clean থাকে |

### ৪.২ একটু বিস্তারিত

#### Python

- সব logic Python-এ — `scripts/` আর `playwright_automation/`।
- Node-ও Playwright দেয়, কিন্তু long loop + JSON quota + LLM call এক জায়গায় Python-এ manage করা সহজ।
- Java/C#-তে DOM selector বদলাতে সময় বেশি লাগে।

#### Playwright + Chromium

- `bot_core.py` browser খোলে, `actions.py` Facebook-এ click/type করে।
- Selenium-এর চেয়ে auto-wait ভালো, session save সহজ, stealth path পরিষ্কার।
- Facebook মূলত Chrome/Chromium-এ test করা — তাই Chromium।

#### stealth

- `stealth_config.py` library patch + নিজের init script inject করে।
- library এক lone, custom script fleet-এ আলাদা fingerprint দেয়।

#### httpx

- `brain.py` → local Ollama, `ai_comment.py` → Gemini, `fleet_status.py` → webhook।
- fleet-এ timeout/retry দরকার — httpx-এ handle করা সহজ।

#### python-dotenv

- dev PC, Docker host, server — তিন জায়গায় আলাদা Ollama address/proxy/key; code touch না করেই `.env` বদলাও।

#### local Ollama (llama3.1:8b)

- brain mode-এর main AI — JSON action, বাংলা/ইংরেজি comment, status post।
- cloud-এ পাঠালে prompt যায় বাইরে + টাকা লাগে; local-এ PC-তেই থাকে।
- `8b` model — ৮–১৬ GB RAM-এ চলে, quality/speed balance ঠিক আছে।
- ৫০০+ bot scale-এ শুধু OpenAI expensive হবে।

#### Gemini (optional)

- Ollama down বা slow হলে comment/caption Gemini দিয়ে।
- structured fleet mode-এ LLM কম লাগে — Gemini must না।

#### Docker

- এক container = এক account, `FLEET_MODE=1`, headless, `profiles/` mount।
- ছোট scale-এ Compose enough; বড় scale-এ K8s — `FLEET_SCALING.md` দেখো।

#### JSON, database না

- account প্রতি আলাদা folder, join/query দরকার নেই।
- debug: `cat profiles/xxx/daily_friend_quota.json` — done।
- analytics weak — fleet `--status` JSON গুলো aggregate করে।

### ৪.৩ `requirements.txt` — package গুলো

| Package | কাজ |
|---------|-----|
| `playwright` | browser automation |
| `playwright-stealth` | detection কমানো |
| `httpx` | Ollama, Gemini, webhook |
| `tzdata` | timezone |
| `python-dotenv` | `.env` load |
| `truststore`, `certifi` | SSL fix |

### ৪.৪ যা **ইচ্ছে করে** use করিনি

| নাই | কেন |
|-----|-----|
| Facebook Graph API | logged-in user-এর মতো feed/friend UI access মেলে না |
| FastAPI / Flask | server দরকার নেই — CLI যথেষ্ট |
| PostgreSQL / Redis | JSON file-ই enough, setup কম |
| Selenium | Playwright এ project-এ better fit |
| শুধু cloud LLM | brain mode-এ cost + latency বেশি |

---

## ৫. ফোল্ডার ও ফাইল

```
bot-agent/
├── README.md
├── pyproject.toml
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
│
├── docs/
│   ├── SYSTEM_DESIGN_EN.md
│   ├── SYSTEM_DESIGN_BN.md    ← তুমি এখানে
│   └── FLEET_SCALING.md
│
├── playwright_automation/     ← মূল library
├── scripts/                   ← command গুলো
├── accounts/                  ← login info (git-এ যাবে না)
└── profiles/                  ← runtime data (git-এ যাবে না)
    └── <account_id>/
        ├── storage_state.json
        ├── daily_*_quota.json
        ├── fleet_status.json
        └── browser/
```

---

## ৬. মূল মডিউলগুলো

### `BaseBot` — browser খোলা

- account প্রতি আলাদা Chromium profile।
- proxy, random user-agent, stealth, timezone set করে।
- cookie `storage_state.json`-এ save/load।

### `AgentSession` — cycle চালানো

- feed memory, quota count, cycle info রাখে।
- Ollama যা বলে (`agent_step`) সেটা browser-এ execute করে।
- friend / post / share phase চালায়।

### `AgentDecision` — Ollama কী বলে

```json
{
  "action": "comment_post",
  "location": "newsfeed",
  "thought_process": "এই পোস্টে রাজনীতি নিয়ে কথা..."
}
```

### `Brain` — local Ollama-র সাথে কথা

- `/api/chat` call করে।
- fleet-এ অনেক bot এক Ollama share করলে `FLEET_OLLAMA_MIN_INTERVAL_SEC` gap দেয় — overload কমায়।

### `ai_comment.py` — text বানানো

- comment, share caption, status post।
- আগে local Ollama, না হলে Gemini।
- `STATUS_BN_RATIO` — কতটা বাংলা, কতটা ইংরেজি post।

---

## ৭. চালানোর দুটো মোড

### Brain mode (default)

প্রতি cycle-এ roughly:

1. **Friend** — কোটা বাকি থাকলে suggestion থেকে profile দেখে, ২০০০+ হলে request (cycle-এ max ১, দিনে ৩–৪)।
2. **Status post** — ফিড scroll, topic বের, post।
3. **Ollama steps** — ৬–৮ বার: page দেখে → JSON → execute।
4. **Share** — দিনে ২০টা না হলে আরো share।
5. একটু wait, save, আবার শুরু।

Ollama বন্ধ থাকলে simple scroll/like fallback চলে।

### Structured mode (`--mode structured`)

fixed ক্রম — LLM কম লাগে, fleet/Docker-এ default:

1. Friend send (+ accept)
2. Feed: scroll → like → comment → share
3. Status post

### Cycle flow (brain mode)

```mermaid
flowchart TD
    A[cycle শুরু] --> B{friend quota বাকি?}
    B -->|হ্যাঁ| C[suggestion browse]
    B -->|না| D
    C --> D{post quota বাকি?}
    D -->|হ্যাঁ| E[ফিড scroll + post]
    D -->|না| F
    E --> F[Ollama steps]
    F --> G{share quota বাকি?}
    G -->|হ্যাঁ| H[share]
    G -->|না| I
    H --> I[save + wait]
    I --> A
```

---

## ৮. ডেটা কীভাবে ঘুরে

### এক step (brain mode)

```mermaid
sequenceDiagram
    participant R as run_agent_brain
    participant E as agent_executor
    participant B as agent_brain
    participant O as local Ollama
    participant A as actions
    participant F as Facebook

    R->>E: agent_step()
    E->>B: পরের action কী?
    B->>O: POST /api/chat
    O-->>B: JSON
    B-->>E: AgentDecision
    E->>A: browser-এ কর
    A->>F: click/type
    F-->>A: done
    E-->>R: step শেষ
```

### comment বানানো

```mermaid
sequenceDiagram
    participant E as agent_executor
    participant AI as ai_comment
    participant O as local Ollama
    participant G as Gemini

    E->>AI: post দিয়ে comment চাই
    AI->>O: generate
    alt Ollama fail
        AI->>G: backup
    end
    AI-->>E: comment text
```

---

## ৯. অ্যাকাউন্ট ও লগইন

### account info কোথায়

1. **`accounts/accounts.json`** — সবচেয়ে ভালো (id, password, cookies, proxy)
2. **`accounts/<id>.env`** — account প্রতি আলাদা env
3. **`cookies.txt`** — পুরনো format, ৩ লাইন per account

### `cookies.txt` format

```
account_id
password
c_user=...; xs=...; datr=...
```

নতুন format-এ নিতে:

```bash
python scripts/migrate_cookies_to_registry.py
```

### login হল কী হয়

1. আগে save করা `storage_state.json` load
2. Facebook খুলে logged in কিনা দেখে
3. না হলে cookie দিয়ে বা password login
4. **checkpoint** (Facebook verify চায়) — সাধারণ mode-এ ৩০ মিনিট wait; `--fleet-mode`-এ skip

### proxy

```
http://user:pass@host:port
```

`accounts.json`, `.env`-এ `PROXY_URL`, বা `--proxy` flag — যেকোনো একটা।

---

## ১০. কোথায় কী save হয় + daily limit

`profiles/<account_id>/` folder:

| ফাইল | কী রাখে |
|------|---------|
| `storage_state.json` | cookie, localStorage |
| `daily_friend_quota.json` | আজ কত friend পাঠিয়েছে (limit ৩–৪) |
| `daily_post_quota.json` | আজ কত post (limit ৩–৫) |
| `daily_share_quota.json` | আজ কত share (limit ২০) |
| `fleet_status.json` | bot চলছে কিনা, error, checkpoint |
| `browser/` | Chromium profile data |

| কাজ | দিনে default |
|-----|-------------|
| Friend request | ৩–৪ |
| Status post | ৩–৫ |
| Share | ২০ |
| Feed (like/comment) | limit নেই, cycle জুড়ে |

নতুন দিন শুরু হলে quota JSON reset হয়।

---

## ১১. AI / LLM কীভাবে কাজে লাগে

| কাজ | আগে | backup |
|-----|-----|--------|
| পরের action | local Ollama | offline scroll/like |
| comment | local Ollama | Gemini |
| share caption | local Ollama | Gemini |
| status post | local Ollama | skip |
| follower count | DOM parse | Ollama |

### local Ollama setup

| Variable | default |
|----------|---------|
| `OLLAMA_HOST` | `127.0.0.1:11434` |
| `OLLAMA_MODEL` | `llama3.1:8b` |

চালু আছে কিনা:

```bash
python scripts/check_ollama.py
```

### Gemini

Ollama fail হলে comment/caption-এ। `.env`-এ `GEMINI_API_KEY` দাও।

### fleet + একটা Ollama

অনেক bot এক PC-র Ollama share করলে `FLEET_OLLAMA_MIN_INTERVAL_SEC=8` — call-এর মাঝে gap, নাহলে overload।

---

## ১২. ব্রাউজার automation ও bot ধরা পড়া এড়ানো

| জিনিস | কী করা হয় |
|--------|-----------|
| Profile | account প্রতি আলাদা Chromium data |
| Screen | default mobile 360×800 |
| User-agent | random rotate |
| Stealth | playwright-stealth + canvas noise |
| Mouse | Bezier curve — সোজা line না |
| Scroll | segment scroll — এক ধাক্কায় না |
| Typing | typo, backspace, pause — `human_behavior.py` |

`.env`-এ `HUMAN_TYPO_RATE`, `HUMAN_RETHINK_RATE` দিয়ে tuning।

---

## ১৩. Facebook-এ কী কী করে

### Friend request

1. Suggestions page
2. হালকা scroll
3. random row click
4. profile ১২–২৮ sec দেখা
5. follower count — DOM বা Ollama
6. ≥ ২০০০ হলে Add Friend
7. daily limit পূর্ণ হলে stop

শুধু friend: `python scripts/send_one_friend.py`

### Status post

1. feed scroll → text snippet জমা
2. local Ollama topic বের করে
3. নিজের মতো লেখা (copy-paste generic না)
4. composer-এ type করে post

২টার কম snippet থাকলে skip।

### Share

1. feed থেকে post বাছাই (story/reel skip)
2. local Ollama বা Gemini caption
3. মানুষের মতো type (typo, pause)
4. confirm → feed-এ ফিরে যাও

---

## ১৪. অনেক বট একসাথে (fleet + Docker)

### PC-তে fleet (`fleet_launcher.py`)

- `accounts.json` থেকে account প্রতি এক process
- start-এ ৩০–১২০ sec gap — একসাথে সব খুললে load বেশি
- crash হলে restart
- `python scripts/fleet_launcher.py --status` — সব bot-এর health

| Phase | max bot | কখন |
|-------|---------|------|
| 1 | 10 | নিজের PC-তে test |
| 2 | 50 | একটা server |
| 3 | 600 | অনেক machine |

### Docker

container প্রতি:

- `FLEET_MODE=1` — headless, checkpoint wait না
- `--mode structured` — কম LLM
- `profiles/` volume mount — session থাকে

```bash
docker compose up --build bot1 bot2 bot3
```

বিস্তারিত RAM/CPU: [FLEET_SCALING.md](FLEET_SCALING.md)

### monitoring

`fleet_status.json` + optional `FLEET_ALERT_WEBHOOK` — checkpoint/crash হলে notify।

---

## ১৫. বাইরের service গুলো

| Service | কীভাবে | কেন |
|---------|--------|-----|
| **local Ollama** | HTTP `/api/chat` | main AI |
| **Gemini** | HTTPS | text backup |
| **Facebook** | Playwright browser | সব UI action |
| **Proxy** | Playwright proxy | account প্রতি আলাদা IP |
| **Webhook** | POST JSON | fleet alert |

---

## ১৬. `.env` ও config

`.env.example` copy করে `.env` বানাও।

### AI

| Variable | default | মানে |
|----------|---------|------|
| `OLLAMA_HOST` | `127.0.0.1:11434` | local Ollama কোথায় |
| `OLLAMA_MODEL` | `llama3.1:8b` | কোন model |
| `GEMINI_API_KEY` | empty | backup API key |

### Facebook behaviour

| Variable | default | মানে |
|----------|---------|------|
| `MIN_AUDIENCE_FRIEND_REQUEST` | `2000` | কম follower হলে friend দেবে না |
| `STATUS_BN_RATIO` | `0.65` | post-এর ~৬৫% বাংলা |

### Fleet

| Variable | default | মানে |
|----------|---------|------|
| `FLEET_MODE` | `0` | `1` = headless worker |
| `FLEET_OLLAMA_MIN_INTERVAL_SEC` | `8.0` | Ollama call gap |
| `FLEET_ALERT_WEBHOOK` | empty | alert URL |
| `PROXY_URL` | — | proxy override |

---

## ১৭. কোন command কী করে

| Command | কাজ |
|---------|-----|
| `python scripts/run_agent_brain.py` | **main** — agent চালু |
| `python scripts/send_one_friend.py` | শুধু friend |
| `python scripts/fleet_launcher.py` | অনেক bot |
| `python scripts/fleet_launcher.py --status` | health check |
| `python scripts/check_ollama.py` | local Ollama OK? |
| `python scripts/unlock_browser_profile.py --kill-chrome` | profile lock fix |

### useful flags

| Flag | default | মানে |
|------|---------|------|
| `--mode` | `brain` | `structured` = fixed pipeline |
| `--account-id` | env | কোন account |
| `--proxy` | env | proxy URL |
| `--fleet-mode` | off | headless worker |
| `--headless` | off | browser দেখাবে না |
| `--skip-friends` | off | friend phase skip |

---

## ১৮. নিরাপত্তা — যা মাথায় রাখবেন

- `.env`, `accounts.json`, `cookies.txt` — **git-এ push করবেন না**।
- fleet-এ account প্রতি **আলাদা proxy** ভালো।
- Facebook checkpoint চাইলে manually verify করতে হতে পারে।
- daily quota কম রাখা হয়েছে — বাড়ালে ban risk বাড়ে।
- automated activity Facebook ToS লঙ্ঘন করতে পারে — **দায় তোমার**।

---

## ১৯. সমস্যা হলে কী করবেন

| সমস্যা | করো |
|--------|-----|
| Ollama connect হয় না | Ollama app চালু? `OLLAMA_HOST=127.0.0.1:11434` |
| Profile locked | `python scripts/unlock_browser_profile.py --kill-chrome` |
| Friend যাচ্ছে না | local Ollama + suggestions page load হচ্ছে? |
| Post হচ্ছে না | আরো cycle চালাও — feed memory বাড়তে হবে (≥ ২ snippet) |
| Checkpoint | browser-এ নিজে verify; agent ৩০ min wait |
| Fleet crash loop | `profiles/<id>/fleet_status.json` পড়ো |

---

*version: `facebook-agent` 0.2.0*

*English: [SYSTEM_DESIGN_EN.md](SYSTEM_DESIGN_EN.md)*
