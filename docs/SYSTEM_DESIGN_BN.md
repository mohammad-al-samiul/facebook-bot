# Facebook Agent — পুরো প্রজেক্ট সিস্টেম ডিজাইন (বাংলা)

এই ডকুমেন্টে `facebook-agent` প্রজেক্টের **সম্পূর্ণ সিস্টেম ডিজাইন** বর্ণনা করা হয়েছে: উদ্দেশ্য, আর্কিটেকচার, কম্পোনেন্ট, ডেটা ফ্লো, persistence, AI ইনtegration, fleet scaling, এবং অপারেশন।

---

## সূচিপত্র

1. [সিস্টেম ওভারভিউ](#১-সিস্টেম-ওভারভিউ)
2. [সিস্টেম কনটেক্সট](#২-সিস্টেম-কনটেক্সট)
3. [আর্কিটেকচার](#৩-আর্কিটেকচার)
4. [টেকনোলজি স্ট্যাক — কেন ও কী কাজে](#৪-টেকনোলজি-স্ট্যাক--কেন-ও-কী-কাজে)
5. [প্রজেক্ট স্ট্রাকচার](#৫-প্রজেক্ট-স্ট্রাকচার)
6. [মূল কম্পোনেন্ট](#৬-মূল-কম্পোনেন্ট)
7. [রান মোড ও কন্ট্রোল ফ্লো](#৭-রান-মোড-ও-কন্ট্রোল-ফ্লো)
8. [ডেটা ফ্লো ডায়াগ্রাম](#৮-ডেটা-ফ্লো-ডায়াগ্রাম)
9. [অ্যাকাউন্ট ও সেশন ম্যানেজমেন্ট](#৯-অ্যাকাউন্ট-ও-সেশন-ম্যানেজমেন্ট)
10. [স্টেট persistence ও দৈনিক কোটা](#১০-স্টেট-persistence-ও-দৈনিক-কোটা)
11. [AI ও LLM ইনtegration](#১১-ai-ও-llm-ইনtegration)
12. [ব্রাউজার অটোমেশন ও স্টেলথ](#১২-ব্রাউজার-অটোমেশন-ও-স্টেলথ)
13. [Facebook কার্যকলাপ ফ্লো](#১৩-facebook-কার্যকলাপ-ফ্লো)
14. [Fleet ও Docker ডিপ্লয়মেন্ট](#১৪-fleet-ও-docker-ডিপ্লয়মেন্ট)
15. [বাহ্যিক ইনtegration](#১৫-বাহ্যিক-ইনtegration)
16. [কনফিগারেশন রেফারেন্স](#১৬-কনফিগারেশন-রেফারেন্স)
17. [CLI এন্ট্রি পয়েন্ট](#১৭-cli-এন্ট্রি-পয়েন্ট)
18. [নিরাপত্তা ও অপারেশনাল নোট](#১৮-নিরাপত্তা-ও-অপারেশনাল-নোট)
19. [সমস্যা সমাধান](#১৯-সমস্যা-সমাধান)

---

## ১. সিস্টেম ওভারভিউ

### ১.১ উদ্দেশ্য

`facebook-agent` একটি **CLI-চালিত Python অটোমেশন সিস্টেম** যা Playwright দিয়ে Chromium নিয়ন্ত্রণ করে **মানুষের মতো Facebook কার্যকলাপ** করে — একবারে একটি অ্যাকাউন্ট (অথবা fleet মোডে অনেকগুলো)।

**ক্ষমতাসমূহ:**

| ক্ষমতা | বর্ণনা |
|--------|--------|
| ফিড এনgage | স্ক্রল, লাইক, রিঅ্যাক্ট, কমেন্ট, শেয়ার |
| স্ট্যাটাস পোস্ট | ফিড মেমোরি থেকে ট্রেন্ডিং টপিক বুঝে মূল মতামত পোস্ট |
| ফ্রেন্ড রিকোয়েস্ট | দিনে সীমিত সংখ্যক — শুধু ২০০০+ ফলোয়ার/ফ্রেন্ড আছে এমন প্রোফাইলে |
| সেশন persistence | রিস্টার্টের পরেও কুকি ও দৈনিক কাউন্টার সংরক্ষিত |
| Multi-account fleet | subprocess launcher বা Docker দিয়ে অনেক বট |

### ১.২ এই সিস্টেম কী নয়

- **ওয়েব সার্ভার নয়** — কোনো REST API, FastAPI, বা Flask backend নেই।
- **ডাটাবেস-ভিত্তিক অ্যাপ নয়** — সব স্টেট `profiles/` ফোল্ডারে JSON ফাইলে।
- **Facebook API ক্লায়েন্ট নয়** — সব কাজ ব্রাউজার UI দিয়ে, যেন একজন আসল ব্যবহারকারী।

### ১.৩ মূল এন্ট্রি পয়েন্ট

```bash
python scripts/run_agent_brain.py
```

---

## ২. সিস্টেম কনটেক্সট

```mermaid
flowchart LR
    subgraph Operator
        U[ব্যবহারকারী / DevOps]
    end

    subgraph facebook_agent["facebook-agent (এই প্রজেক্ট)"]
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

    U -->|কমান্ড চালায়| CLI
    PA -->|HTTP /api/chat| OLL
    PA -->|HTTPS fallback| GEM
    PA -->|Playwright browser| FB
    PA -->|ঐচ্ছিক| PX
    PA -->|ঐচ্ছিক alert| WH
```

---

## ৩. আর্কিটেকচার

### ৩.১ স্তরভিত্তিক আর্কিটেকচার

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

### ৩.২ কম্পোনেন্ট ইন্টারঅ্যাকশন (একটি বট)

```
                    ┌─────────────────────────┐
                    │  scripts/run_agent_brain │
                    │  (CLI, লগইন, লুপ)       │
                    └────────────┬────────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         ▼                       ▼                       ▼
  account_registry         agent_executor           BaseBot
  account_session          (সাইকেল, কোটা)        (Playwright)
  facebook_login                 │                       │
         │           ┌──────────┴──────────┐            │
         │           ▼                     ▼            │
         │      agent_brain            actions ◄─────────┘
         │      (JSON সিদ্ধান্ত)     (UI অটোমেশন)
         │           │
         │           ▼
         │      brain.py ──HTTP──► Ollama (স্থানীয়)
         │           │
         │           ▼
         └──────► ai_comment.py ──HTTP──► Gemini (ঐচ্ছিক)
                                   │
                                   ▼
                            Facebook (ব্রাউজার)
```

### ৩.৩ স্তর অনুযায়ী দায়িত্ব

| স্তর | মডিউল | কাজ |
|------|-------|-----|
| এন্ট্রি | `run_agent_brain.py` | CLI args, ব্রাউজার চালু, অসীম লুপ |
| এন্ট্রি | `fleet_launcher.py` | Nটি subprocess বট spawn, auto-restart |
| এন্ট্রি | `docker_entrypoint.py` | কনটেইনার entry: stagger → agent চালু |
| সেশন | `account_registry.py` | JSON, `.env`, `cookies.txt` থেকে credentials |
| সেশন | `account_session.py` | কুকি পার্স, লগইন চেক, checkpoint wait |
| সেশন | `facebook_login.py` | লগইন, checkpoint URL detect |
| অর্কেস্ট্রেশন | `agent_executor.py` | দৈনিক কোটা, brain steps, structured cycles |
| সিদ্ধান্ত | `agent_brain.py`, `brain.py` | Ollama-কে পরবর্তী JSON action |
| কনটেন্ট | `ai_comment.py` | কমেন্ট, ক্যাপশন, স্ট্যাটাস |
| ব্রাউজার | `actions.py`, `bot_core.py` | ক্লিক, স্ক্রল, টাইপ, রিঅ্যাক্ট |
| সোশ্যাল গ্রাফ | `facebook_graph.py` | ফ্রেন্ড সাজেশন, অডিয়েন্স চেক |
| ফিড | `post_engagement.py` | পোস্ট বাছাই, fingerprint dedup |
| প্রোফাইল | `profile_engagement.py` | friend send-এর আগে profile stalk |
| স্টেলথ | `stealth_config.py`, `human_behavior.py` | ফিঙ্গারপ্রিন্ট, প্রাকৃতিক টাইপিং |
| Fleet ops | `fleet_status.py` | health JSON + webhook alerts |
| Profile lock | `browser_profile.py` | Chromium lock ফাইল মুক্ত |

---

## ৪. টেকনোলজি স্ট্যাক — কেন ও কী কাজে

প্রতিটি **technology কেন নেওয়া হয়েছে** এবং **প্রজেক্টে ঠিক কী কাজ করে** — তা নিচে বর্ণনা করা হয়েছে।

### ৪.১ সারসংক্ষেপ টেবিল

| Technology | প্রজেক্টে কী কাজ করে | কেন ব্যবহার |
|------------|---------------------|------------|
| **Python ≥ 3.10** | সব script ও `playwright_automation` লাইব্রেরি | async support, দ্রুত prototyping, automation ecosystem |
| **Playwright (Chromium)** | বাস্তব ব্রাউজার চালায় — click, scroll, type, DOM, cookies | Facebook-এ feed/comment/share-এর public API নেই; UI দিয়ে real user-এর মতো |
| **playwright-stealth** | automation fingerprint (`navigator.webdriver` ইত্যাদি) লুকায় | সাধারণ Playwright bot-detection signal কমায় |
| **Custom stealth scripts** | canvas/WebGL/WebRTC noise ও init scripts | লাইব্রেরির বাইরে extra layer; mobile Facebook-এর জন্য |
| **httpx** | Ollama, Gemini, fleet webhook-এ async HTTP | Playwright async loop-এ `requests`-এর চেয়ে সহজ |
| **python-dotenv** | startup-এ `.env` থেকে env variable load | secret ও tuning source code-এ না রেখে এক ফাইলে |
| **tzdata** | browser timezone (যেমন `Asia/Dhaka`) | Windows-এ zoneinfo incomplete হতে পারে |
| **truststore + certifi** | Gemini HTTPS-এ TLS/SSL ঠিক রাখে | corporate proxy বা Windows-এ cert error এড়ায় |
| **Ollama + llama3.1:8b** | local LLM — JSON action, comment, caption, status | offline, cloud cost নেই, desktop GPU/CPU-তে low latency |
| **Google Gemini** | Ollama fail হলে text generation fallback | fleet-এ local GPU ছাড়াই comment/caption চালু রাখে |
| **Docker + Compose** | bot প্রতি container, staggered startup | server-এ অনেক account scale; reproducible runtime |
| **JSON (`profiles/`)** | session, daily quota, fleet health store | DB setup নেই; debug/backup/mount সহজ |
| **asyncio** | Playwright ও HTTP এক process-এ concurrent | Playwright Python async-native |
| **setuptools / pyproject.toml** | `playwright_automation` package install | `scripts/` থেকে clean import |

### ৪.২ বিস্তারিত

#### Python 3.10+

- **কাজ:** `scripts/`, `playwright_automation/`, fleet launcher, Docker entrypoint।
- **Node.js কেন নয়?** Playwright দুটোতেই আছে, কিন্তু long-running loop + JSON quota + LLM client এক codebase-এ Python সুবিধাজনক।
- **Java/C# কেন নয়?** DOM heuristic ও prompt পরিবর্তনে iteration ধীর।

#### Playwright + Chromium

- **কাজ:** `bot_core.py` persistent context; `actions.py` সব Facebook UI step।
- **Handles:** navigation, selectors, mobile viewport, proxy, `storage_state.json`।
- **Selenium কেন নয়?** auto-wait, persistent context, modern API, stealth path ভালো।
- **Chromium কেন?** Facebook Chrome/Chromium-এ heavily tested।

#### playwright-stealth + custom scripts

- **কাজ:** `stealth_config.py` — library patch + page load-এর আগে init scripts।
- **দুটো কেন?** library common leak ঢেকে; custom scripts fleet bot-এ extra variation।

#### httpx

- **কাজ:** `brain.py` → Ollama; `ai_comment.py` → Gemini; `fleet_status.py` → webhook।
- **`urllib` কেন নয়?** async ও timeout/error handling fleet throttling-এ দরকার।

#### python-dotenv

- **কাজ:** project-root `.env` load।
- **hard-code কেন নয়?** dev PC, Docker host, fleet server — আলাদা Ollama/proxy/key, code change ছাড়াই।

#### tzdata

- **কাজ:** CLI `--timezone` (ডিফল্ট `Asia/Dhaka`)।
- **কেন?** system TZ vs Facebook session TZ mismatch detection signal হতে পারে।

#### truststore + certifi

- **কাজ:** Windows/MITM proxy-তে Gemini HTTPS SSL।
- **কেন?** Windows Python cert store কখনো browser TLS-এর মতো কাজ করে না।

#### Ollama (llama3.1:8b)

- **কাজ:** primary brain — JSON decision, বাংলা/ইংরেজি comment, trending status।
- **local LLM কেন?** privacy, API billing নেই, Facebook ছাড়া slow internet-এও চলে।
- **llama3.1:8b কেন?** 8–16 GB RAM-এ speed/quality balance; fleet-এ shared GPU-তে manageable।
- **শুধু OpenAI কেন নয়?** 500+ bot scale-এ cost ও external uptime dependency।

#### Google Gemini (ঐচ্ছিক)

- **কাজ:** Ollama error/timeout-এ `ai_comment.py` fallback।
- **ঐচ্ছিক কেন?** structured fleet mode minimal LLM-এ চলতে পারে।

#### Docker + Docker Compose

- **কাজ:** `Dockerfile`, `docker-compose.yml`, generated fleet compose।
- **দেয়:** per-account `ACCOUNT_ID`, `FLEET_MODE=1`, headless structured agent, `profiles/` volume।
- **শুধু K8s কেন নয়?** Phase 1–2-তে Compose যথেষ্ট; Phase 3-তে K8s — `FLEET_SCALING.md`।

#### JSON persistence (ডাটাবেস নেই)

- **কাজ:** `profiles/<account_id>/` — quota, cookies, fleet status।
- **PostgreSQL/SQLite কেন নয়?** account প্রতি single-writer, join নেই; `cat` দিয়ে debug সহজ।
- **trade-off:** centralized analytics weak; fleet `--status` JSON aggregate করে।

#### asyncio

- **কাজ:** `asyncio.run()`; page action ও Ollama call block না করে interleave।
- **threaded Selenium কেন নয়?** reasoning কঠিন; Playwright Python async-first।

### ৪.৩ Dependencies (`requirements.txt`)

| Package | Version | ব্যবহার |
|---------|---------|---------|
| `playwright` | ≥ 1.49.0 | Browser automation |
| `playwright-stealth` | ≥ 2.0.0 | Anti-detection |
| `httpx` | ≥ 0.27.0 | Ollama, Gemini, webhooks |
| `tzdata` | ≥ 2024.1 | Timezone data |
| `python-dotenv` | ≥ 1.0.0 | `.env` load |
| `truststore` | ≥ 0.10.0 | OS trust store TLS |
| `certifi` | ≥ 2024.7.4 | CA bundle fallback |

### ৪.৪ যা ইচ্ছাকৃতভাবে ব্যবহার করা হয়নি

| ব্যবহার হয়নি | কারণ |
|--------------|------|
| Facebook Graph API | logged-in user-এর মতো feed/friend/composer access নেই |
| FastAPI / Flask | HTTP server দরকার নেই; CLI + subprocess fleet যথেষ্ট |
| PostgreSQL / Redis | per-bot JSON state যথেষ্ট; ops overhead এড়ায় |
| Selenium | Playwright persistence ও stealth ভালো |
| শুধু cloud LLM | brain mode-এ cost ও latency বেশি |

---

## ৫. প্রজেক্ট স্ট্রাকচার

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
│   ├── SYSTEM_DESIGN_EN.md        # ইংরেজি সংস্করণ
│   ├── SYSTEM_DESIGN_BN.md        # এই ডকুমেন্ট
│   └── FLEET_SCALING.md
│
├── playwright_automation/         # মূল লাইব্রেরি
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
├── scripts/                       # CLI এন্ট্রি পয়েন্ট
│   ├── run_agent_brain.py         # ★ মূল agent runner
│   ├── send_one_friend.py
│   ├── fleet_launcher.py
│   ├── check_ollama.py
│   └── ...
│
├── accounts/                      # Credentials (gitignored)
└── profiles/                      # Runtime state (gitignored)
    └── <account_id>/
        ├── storage_state.json
        ├── daily_*_quota.json
        ├── fleet_status.json
        └── browser/
```

---

## ৬. মূল কম্পোনেন্ট

### ৬.১ `BaseBot` (`bot_core.py`)

- প্রতি অ্যাকাউন্টে **persistent Chromium context** তৈরি করে।
- Proxy, user-agent rotation, stealth scripts, timezone প্রয়োগ করে।
- `storage_state.json` load/save করে কুকি persistence রাখে।

### ৬.২ `AgentSession` (`agent_executor.py`)

- Runtime state: feed memory snippets, quota counters, cycle metadata।
- প্রতিটি Ollama সিদ্ধান্তের জন্য `agent_step()` execute করে।
- Structured cycles ও daily phases (friend, post, share) চালায়।

### ৬.৩ `AgentDecision` (`agent_brain.py`)

Ollama strict JSON দিয়ে পরবর্তী action বলে:

```json
{
  "action": "comment_post",
  "location": "newsfeed",
  "thought_process": "এই পোস্টে স্থানীয় রাজনীতি নিয়ে আলোচনা..."
}
```

### ৬.৪ `Brain` (`brain.py`)

- Ollama `/api/chat` ও `/api/tags`-এর HTTP client।
- Fleet throttling: `FLEET_OLLAMA_MIN_INTERVAL_SEC` দিয়ে shared Ollama overload এড়ায়।

### ৬.৫ `ai_comment.py`

- কমেন্ট, share caption, status post generate করে।
- Primary: Ollama। Fallback: Gemini API।
- `STATUS_BN_RATIO` দিয়ে বাংলা/ইংরেজি ভাষা নিয়ন্ত্রণ।

---

## ৭. রান মোড ও কন্ট্রোল ফ্লো

### ৭.১ Brain mode (ডিফল্ট)

প্রতিটি **সাইকেল**:

1. **Friend phase** (কোটা বাকি): ১৫–২৫ suggestion row stalk, ১২–২৮ সেকেন্ড browse, অডিয়েন্স ≥ ২০০০ হলে request। **সাইকেলে ১**, **দিনে ৩–৪**।
2. **Status post phase** (কোটা বাকি): ফিড স্ক্রল, snippet জমা, trending topic, পোস্ট publish।
3. **Ollama steps** (ডিফল্ট ৬–৮): observe → JSON → execute।
4. **Share top-up** — দৈনিক ২০ শেয়ার পূরণ না হলে।
5. বিরতি, state save, আবার শুরু।

Ollama offline হলে offline fallback (scroll + সাধারণ engagement)।

### ৭.২ Structured mode (`--mode structured`)

নির্দিষ্ট pipeline — কম LLM load, fleet launcher ও Docker-এ ব্যবহৃত:

1. Friend send + accept (ঐচ্ছিক)
2. Feed rounds: scroll → like → comment → share
3. Status post

### ৭.৩ Brain cycle flowchart

```mermaid
flowchart TD
    A[সাইকেল শুরু] --> B{Friend কোটা বাকি?}
    B -->|হ্যাঁ| C[Random stalk suggestions]
    B -->|না| D
    C --> D{Post কোটা বাকি?}
    D -->|হ্যাঁ| E[ফিড স্ক্রল + trending post]
    D -->|না| F
    E --> F[Ollama steps x N]
    F --> G{Share কোটা বাকি?}
    G -->|হ্যাঁ| H[Timeline-এ share]
    G -->|না| I
    H --> I[Save + pause]
    I --> A
```

---

## ৮. ডেটা ফ্লো ডায়াগ্রাম

### ৮.১ একক action step (brain mode)

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
    E->>A: page-এ action execute
    A->>F: Playwright UI interaction
    F-->>A: DOM updated
    A-->>E: result
    E-->>R: step complete
```

### ৮.২ কমেন্ট generation ফ্লো

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
    AI->>O: text generate
    alt Ollama fail
        AI->>G: fallback generateContent
    end
    AI-->>E: comment text
    E->>A: comment_on_post(human typing)
```

---

## ৯. অ্যাকাউন্ট ও সেশন ম্যানেজমেন্ট

### ৯.১ অ্যাকাউন্ট সোর্স (অগ্রাধিকার ক্রম)

1. **`accounts/accounts.json`** (সুপারিশকৃত) — `id`, `password`, `cookies`, `proxy`
2. **`accounts/<account_id>.env`** — per-account env ফাইল
3. **Legacy `cookies.txt`** — প্রতি অ্যাকাউন্ট ৩ লাইন

### ৯.২ Legacy `cookies.txt` ফরম্যাট

```
account_id
password
c_user=...; xs=...; datr=...
```

Migration:

```bash
python scripts/migrate_cookies_to_registry.py
```

### ৯.৩ লগইন ফ্লো

1. `storage_state.json` load (যদি থাকে)
2. Facebook-এ navigate — logged in কিনা চেক
3. না হলে: cookies seed বা `stealthy_facebook_login()`
4. Checkpoint detect হলে: ৩০ মিনিট manual completion wait (`--fleet-mode`-এ skip)

### ৯.৪ Proxy

`accounts.json`, `PROXY_URL` env, বা `--proxy` CLI:

```
http://user:pass@host:port
```

---

## ১০. স্টেট persistence ও দৈনিক কোটা

সব স্টেট `profiles/<account_id>/`-এ:

| ফাইল | উদ্দেশ্য |
|------|---------|
| `storage_state.json` | Chromium cookies + localStorage |
| `daily_friend_quota.json` | Friend request (৩–৪/দিন) |
| `daily_post_quota.json` | Status post (৩–৫/দিন) |
| `daily_share_quota.json` | Share (২০/দিন) |
| `fleet_status.json` | Bot state, PID, errors, checkpoint |
| `browser/` | Chromium user data directory |

### দৈনিক কোটা (ডিফল্ট)

| কার্যকলাপ | ডিফল্ট কোটা | নোট |
|-----------|-------------|------|
| Friend request | ৩–৪ / দিন | ≥ ২০০০ friends/followers |
| Status post | ৩–৫ / দিন | Feed memory থেকে topic |
| Share | ২০ / দিন | Human-typed caption |
| Feed engagement | Continuous | Like, comment, share |

কোটা প্রতিদিন calendar date অনুযায়ী reset হয়।

---

## ১১. AI ও LLM integration

### ১১.১ Task routing

| কাজ | Primary | Fallback |
|-----|----------|----------|
| পরবর্তী action | Ollama | Offline scroll/like |
| কমেন্ট | Ollama | Gemini API |
| Share caption | Ollama | Gemini API |
| Status post | Ollama | Skip |
| Profile audience | DOM parsing | Ollama text read |

### ১১.২ Ollama কনফিগ

| Variable | Default |
|----------|---------|
| `OLLAMA_HOST` | `127.0.0.1:11434` |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `llama3.1:8b` |

চেক:

```bash
python scripts/check_ollama.py
```

### ১১.৩ Gemini fallback

Ollama fail হলে comment/caption-এ ব্যবহৃত। `.env`-এ `GEMINI_API_KEY` সেট করুন।

### ১১.৪ Fleet LLM throttling

অনেক বট এক Ollama share করলে `FLEET_OLLAMA_MIN_INTERVAL_SEC` (ডিফল্ট ৮s) API call-এর মধ্যে gap রাখে।

---

## ১২. ব্রাউজার অটোমেশন ও স্টেলথ

| বৈশিষ্ট্য | Implementation |
|----------|----------------|
| Persistent profile | অ্যাকাউন্টভিত্তik Chromium user data |
| Viewport | Mobile 360×800 (ডিফল্ট) বা desktop |
| User agent | `user_agent_rotation.py` দিয়ে random |
| Stealth | playwright-stealth + canvas/WebGL noise |
| Mouse | Bezier curves |
| Scroll | Segment-based human scroll |
| Typing | Typo, backspace, pause — `human_behavior.py` |

---

## ১৩. Facebook কার্যকলাপ ফ্লো

### ১৩.১ Friend request ফ্লো

1. Friend suggestions page খোলা
2. ৫ বার হালকা scroll
3. Random suggestion row click (mobile-safe)
4. Profile ১২–২৮ সেকেন্ড browse
5. Friends/followers count (DOM + Ollama)
6. ≥ ২০০০ হলে Add Friend
7. দৈনিক cap-এ থামা

Utility: `python scripts/send_one_friend.py`

### ১৩.২ Status post ফ্লো

1. Feed scroll → snippet জমা
2. Ollama দিয়ে trending topic
3. মূল মতামত লেখা
4. Composer-এ human-type করে publish

২টির কম snippet থাকলে skip।

### ১৩.৩ Share ফ্লো

1. Feed post বাছাই (stories/reels skip)
2. Ollama/Gemini caption
3. Human typing — typo, backspace, pause
4. Confirm → feed-এ ফিরে যাওয়া

---

## ১৪. Fleet ও Docker ডিপ্লয়মেন্ট

### ১৪.১ Host-based fleet

- `accounts.json` থেকে প্রতি অ্যাকাউন্টে একটি subprocess
- Staggered startup (৩০–১২০s gap)
- Crash-এ auto-restart
- `--status` → সব `fleet_status.json` পড়ে

Phase limits:

| Phase | Max bots | ব্যবহার |
|-------|----------|---------|
| 1 | 10 | এক PC-তে verify |
| 2 | 50 | Single server |
| 3 | 600 | Distributed |

```bash
python scripts/fleet_launcher.py --max-bots 10 --phase 1
python scripts/fleet_launcher.py --status
```

### ১৪.২ Docker fleet

প্রতি container-এ:

- `FLEET_MODE=1` (headless, manual checkpoint wait নেই)
- `--mode structured` (Docker ডিফল্ট)
- Staggered startup
- Host `profiles/` mount

```bash
docker compose up --build bot1 bot2 bot3
python scripts/generate_compose_services.py --count 10
```

বিস্তারিত: [FLEET_SCALING.md](FLEET_SCALING.md)

### ১৪.৩ Fleet monitoring

`fleet_status.py` health JSON লেখে। `FLEET_ALERT_WEBHOOK` checkpoint/crash-এ alert পাঠায়।

---

## ১৫. বাহ্যিক integration

| সেবা | প্রোটোকল | ব্যবহার |
|------|----------|---------|
| **Ollama** | HTTP `/api/chat` | Primary LLM |
| **Google Gemini** | HTTPS | Comment/caption fallback |
| **Facebook** | Browser (Playwright) | সব social action UI দিয়ে |
| **HTTP Proxy** | Playwright proxy | Per-account IP isolation |
| **Webhook** | HTTP POST JSON | Fleet alerts |

---

## ১৬. কনফিগারেশন রেফারেন্স

`.env.example` → `.env` কপি করুন।

### AI / LLM

| Variable | Default | উদ্দেশ্য |
|----------|---------|---------|
| `OLLAMA_HOST` | `127.0.0.1:11434` | Ollama host:port |
| `OLLAMA_MODEL` | `llama3.1:8b` | Model name |
| `GEMINI_API_KEY` | (empty) | Gemini fallback |

### Facebook behaviour

| Variable | Default | উদ্দেশ্য |
|----------|---------|---------|
| `MIN_AUDIENCE_FRIEND_REQUEST` | `2000` | Friend send-এর min audience |
| `HUMAN_TYPO_RATE` | `0.045` | Typo probability |
| `STATUS_BN_RATIO` | `0.65` | বাংলা vs ইংরেজি status ratio |

### Fleet / Docker

| Variable | Default | উদ্দেশ্য |
|----------|---------|---------|
| `FLEET_MODE` | `0` | Headless fleet worker |
| `FLEET_OLLAMA_MIN_INTERVAL_SEC` | `8.0` | Ollama call gap |
| `FLEET_ALERT_WEBHOOK` | (empty) | Alert webhook |
| `PROXY_URL` | — | Proxy override |

---

## ১৭. CLI এন্ট্রি পয়েন্ট

| কমান্ড | উদ্দেশ্য |
|--------|---------|
| `python scripts/run_agent_brain.py` | **মূল agent** |
| `python scripts/send_one_friend.py` | শুধু friend request |
| `python scripts/fleet_launcher.py` | Nটি bot subprocess |
| `python scripts/fleet_launcher.py --status` | Fleet health |
| `python scripts/check_ollama.py` | Ollama verify |
| `python scripts/unlock_browser_profile.py --kill-chrome` | Profile lock fix |

### মূল CLI flags

| Flag | Default | উদ্দেশ্য |
|------|---------|---------|
| `--mode` | `brain` | `brain` বা `structured` |
| `--account-id` | env | Account ID |
| `--proxy` | env | HTTP proxy |
| `--fleet-mode` | off | Worker mode |
| `--headless` | off | Headless Chromium |
| `--skip-friends` | off | Friend activity skip |

---

## ১৮. নিরাপত্তা ও অপারেশনাল নোট

- **Credentials** — `.env`, `accounts.json`, `cookies.txt` commit করবেন না (gitignored)।
- **Proxy** — Fleet-এ প্রতি অ্যাকাউন্টে আলাদা residential/mobile proxy ব্যবহার করুন।
- **Checkpoint** — Facebook manual verification চাইতে পারে; non-fleet mode-এ agent wait করে।
- **Rate limits** — দৈনিক কোটা intentionally conservative; সাবধানে adjust করুন।
- **Terms of Service** — Automated activity Facebook ToS লঙ্ঘন করতে পারে; দায় ব্যবহারকারীর।

---

## ১৯. সমস্যা সমাধান

| সমস্যা | সমাধান |
|--------|---------|
| Ollama unreachable | Ollama চালু; `OLLAMA_HOST=127.0.0.1:11434` |
| Profile locked | `python scripts/unlock_browser_profile.py --kill-chrome` |
| ০ friend send | Ollama + suggestions page চেক |
| Status post নেই | আরো cycle — memory বাড়তে হবে (≥ ২ snippet) |
| Checkpoint | Browser-এ manual verify; agent ৩০ মিনিট wait |
| Fleet crash loop | `profiles/<id>/fleet_status.json` দেখুন |

---

*ডকুমেন্ট সংস্করণ: `facebook-agent` 0.2.0-এর সাথে মিল রেখে*

*English version: [SYSTEM_DESIGN_EN.md](SYSTEM_DESIGN_EN.md)*
