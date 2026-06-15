# 🤖 hAI.FinOro

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=for-the-badge&logo=flask&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![eToro](https://img.shields.io/badge/eToro-API-00C851?style=for-the-badge&logo=etoro&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-OpenAI_compatible-412991?style=for-the-badge&logo=openai&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Beta-orange?style=for-the-badge)

**AI-powered Trading Agent for eToro — LLM signal generation, live market dashboard, SMA analysis**

[🚀 Quick Start](#-quick-start) · [📖 Docs](#-architecture) · [⚙️ Config](#-configuration) · [🔐 Security](#-security)

</div>

---

## ✨ Features

| Feature | Details |
|---|---|
| 📊 **Live Dashboard** | 6 instruments (BTC, ETH, Gold, Oil, EUR/USD, GBP/USD) |
| 🤖 **LLM Signals** | OpenAI-compatible endpoint · model `finance` · ONE_DAY candles |
| 📈 **SMA Filter** | SMA20 vs SMA50 trend pre-filter before LLM call |
| 💼 **Portfolio View** | Real-account positions · P&L · CSV export |
| ⭐ **Watchlist** | eToro default watchlist display |
| 🔍 **Auto ID Resolver** | Finds & saves instrument IDs via eToro Search API |
| 🔐 **Secret Scanning** | No keys in source · config.json excluded from git |
| 🐳 **Docker Ready** | One-command deploy |

---

## 🏗 Architecture

```
hAI.FinOro/
├── app.py              # Flask app – main entry point
├── config.json         # 🔒 NOT in git – see config.example.json
├── config.example.json # Template with all required keys
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container definition
├── docker-compose.yml  # One-command deploy
└── docs/
    └── index.html      # GitHub Pages landing page
```

### Agent Stages

```
┌─────────────────────────────────────────────────────┐
│  Stage 1 ✅  Observe Mode                           │
│  → Fetch price + candles → SMA log → no LLM call   │
├─────────────────────────────────────────────────────┤
│  Stage 2 ✅  Trade Signal Mode (ACTIVE)             │
│  → SMA pre-filter → LLM analysis → BUY/SELL/HOLD   │
├─────────────────────────────────────────────────────┤
│  Stage 3 🔒  Live Orders (LOCKED)                   │
│  → Requires write key + explicit code unlock        │
└─────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites
- Docker + Docker Compose
- eToro Public API key (read-only)
- OpenAI-compatible LLM endpoint

### 1. Clone
```bash
git clone https://github.com/jbkunama1/hAI.FinOro.git
cd hAI.FinOro
```

### 2. Configure
```bash
cp config.example.json config.json
nano config.json  # fill in your keys
```

### 3. Deploy
```bash
docker compose up --build -d
```

### 4. Resolve Instrument IDs *(first run only)*
```
http://localhost:5000/debug/instruments
→ Click "💾 Save IDs to config.json"
```

### 5. Open Dashboard
```
http://localhost:5000
```

---

## ⚙️ Configuration

Copy `config.example.json` → `config.json` and fill in:

```json
{
  "API_KEY":    "your-etoro-public-api-key",
  "USER_KEY":   "your-etoro-user-key",
  "API_URL":    "https://public-api.etoro.com/api/v1",
  "SANDBOX":    false,
  "LLM_BASE_URL": "https://your-llm-endpoint/v1",
  "LLM_MODEL":    "finance",
  "LLM_API_KEY":  "none"
}
```

> ⚠️ `config.json` is in `.gitignore` — **never commit your keys!**

Instrument IDs are auto-populated via `/debug/instruments`.

---

## 🔐 Security

| Risk | Mitigation |
|---|---|
| API key exposure | `config.json` in `.gitignore`, never hardcoded |
| Accidental orders | Stage 3 locked behind commented-out code block |
| Unvalidated input | All form inputs sanitized + range-clamped |
| SSRF via LLM URL | LLM URL only set via config file, not user input |
| Threaded state race | `agent_state` dict ops are GIL-protected in CPython |
| Unlimited log growth | Log capped at 50 entries |
| LLM timeout DoS | All LLM calls have 60s timeout |

> 🔒 **Production recommendation:** Add authentication (e.g. Flask-Login or nginx basic auth) before exposing to the internet.

---

## 📡 API Endpoints

| Route | Method | Description |
|---|---|---|
| `/` | GET | Live dashboard |
| `/portfolio` | GET | Open positions |
| `/portfolio.csv` | GET | CSV export |
| `/watchlist` | GET | Default watchlist |
| `/agent` | GET | Agent control panel |
| `/agent/start` | POST | Start agent loop |
| `/agent/stop` | POST | Stop agent loop |
| `/agent/tick` | POST | Manual single tick |
| `/agent/config` | POST | Update mode/interval |
| `/agent/clear-log` | POST | Clear log |
| `/debug/instruments` | GET | Resolve & show IDs |
| `/debug/save-ids` | POST | Save IDs to config |
| `/admin` | GET | System status |

---

## 🛠 Development

```bash
# Local run without Docker
pip install -r requirements.txt
python app.py
```

```bash
# Rebuild Docker after changes
docker compose down && docker compose up --build -d

# View logs
docker compose logs -f
```

---

## ⚠️ Disclaimer

> This project is for **educational purposes only**.
> It does **not** constitute financial advice.
> Trading involves significant risk of loss.
> Always verify signals manually before acting.

---

## 📄 License

MIT © 2026 [therealteacher](https://github.com/jbkunama1)
