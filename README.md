# hAI.FinOro 🤖

> **Minimalistischer autonomer Trading-Agent für eToro Agent-Portfolios**  
> Self-hosted · Docker · LLM-Signal-Generierung · Flask Web-UI

---

## Features

- 📊 **Live-Dashboard** – Kurse, Positionen, PnL in Echtzeit
- 🤖 **LLM-Agent** – Kauf/Verkauf-Signale über OpenAI-kompatible API
- ⚙️ **Web-Config** – Alle Einstellungen über Browser-UI konfigurierbar
- 🔍 **Instrument-Suche** – eToro Instrument-IDs per Suchfunktion finden
- 🧪 **Sandbox-Modus** – Testmodus ohne echte Orders
- 🐳 **Docker-ready** – Ein Befehl zum Starten
- 🐍 **Python 3.9+** kompatibel

---

## Schnellstart

### 1. Repository klonen

```bash
git clone https://github.com/jbkunama1/hAI.FinOro.git
cd hAI.FinOro
```

### 2. Config anlegen

```bash
cp config.example.json config.json
```

Dann `config.json` bearbeiten und eigene Keys eintragen (oder direkt über die Web-UI unter `/config`):

| Key | Beschreibung |
|---|---|
| `API_KEY` | eToro Public API Key |
| `USER_KEY` | eToro User Key (JWT-Token) |
| `API_URL` | eToro API Endpunkt (Default: `https://public-api.etoro.com/api/v1`) |
| `SANDBOX` | `true` = kein echtes Trading |
| `LLM_BASE_URL` | OpenAI-kompatibler LLM-Endpunkt (z.B. Ollama, 9Router, OpenAI) |
| `LLM_MODEL` | Modellname (z.B. `finance`, `llama3`, `gpt-4o`) |
| `LLM_API_KEY` | API-Key für den LLM-Endpunkt |
| `MODE` | `observe` (nur beobachten) oder `trade` (echte Orders) |
| `INTERVAL` | Agent-Tick-Interval in Sekunden (60–86400) |
| `TRADE_AMOUNT` | Betrag pro Trade in € |
| `BTC_INSTRUMENT_ID` | eToro Instrument-ID für Bitcoin (Default: `100134`) |
| `ETH_INSTRUMENT_ID` | eToro Instrument-ID für Ethereum (Default: `100125`) |
| `GOLD_INSTRUMENT_ID` | eToro Instrument-ID für Gold (Default: `559`) |
| `OIL_INSTRUMENT_ID` | eToro Instrument-ID für Öl (Default: `784`) |
| `EURUSD_INSTRUMENT_ID` | EUR/USD (Default: `1`) |
| `GBPUSD_INSTRUMENT_ID` | GBP/USD (Default: `2`) |

### 3. Starten

```bash
docker compose up --build -d
```

Die Web-UI ist erreichbar unter: **http://localhost:5000**

---

## Web-UI Seiten

| Route | Beschreibung |
|---|---|
| `/` | Dashboard – Live-Kurse & Positionen |
| `/config` | Konfiguration – Keys, LLM, Trade-Einstellungen |
| `/agent` | Agent-Status, manueller Tick, Log |
| `/orders` | Offene & vergangene Orders |
| `/debug` | Instrument-Suche & ID-Verwaltung |

---

## Instrument-IDs finden

Unter **`/debug`** → *Instrument suchen* kannst du nach Name oder Symbol suchen (z.B. `Tesla`, `TSLA`, `BTC`).
Die gefundene ID lässt sich direkt als Standard-Instrument speichern.

---

## Docker Compose

```yaml
services:
  haifin:
    build: .
    ports:
      - "5000:5000"
    volumes:
      - ./config.json:/app/config.json
    restart: unless-stopped
```

---

## Anforderungen

- Docker + Docker Compose
- eToro Agent-Portfolio mit API-Zugang
- OpenAI-kompatibler LLM-Endpunkt (Ollama lokal, 9Router, OpenAI, etc.)

---

## Lokale Entwicklung (ohne Docker)

```bash
pip install -r requirements.txt
python app.py
```

---

## Sicherheitshinweis

- `config.json` enthält deine API-Keys → **niemals committen**
- `.gitignore` schließt `config.json` bereits aus
- Setze einen Reverse-Proxy (nginx) mit Auth vor die Web-UI wenn öffentlich erreichbar

---

## Lizenz

MIT – siehe [LICENSE](LICENSE)

---

*Built with ❤️ in Karlsruhe*
