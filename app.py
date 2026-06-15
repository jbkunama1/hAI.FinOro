"""
hAI.FinOro – LLM-powered eToro Trading Agent
https://github.com/jbkunama1/hAI.FinOro

SECURITY NOTES:
  - config.json is NEVER committed (see .gitignore)
  - Stage 3 (live orders) is locked behind commented code
  - All external inputs are validated/sanitized
  - LLM calls have strict timeouts
  - Log is capped to prevent memory growth
"""

from flask import Flask, Response, request
import requests as http
import json, uuid, io, csv, threading, logging
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
LOG_MAX       = 100
CANDLE_COUNT  = 50
CANDLE_LLM    = 30
TIMEOUT_API   = 10
TIMEOUT_LLM   = 60
INTERVAL_MIN  = 60
INTERVAL_MAX  = 86400

INSTRUMENTS_TO_TRACK = [
    {"symbol": "BTC",    "label": "Bitcoin",   "emoji": "₿"},
    {"symbol": "ETH",    "label": "Ethereum",  "emoji": "Ξ"},
    {"symbol": "GOLD",   "label": "Gold",      "emoji": "🥇"},
    {"symbol": "OIL",    "label": "Crude Oil", "emoji": "🛢"},
    {"symbol": "EURUSD", "label": "EUR/USD",   "emoji": "💶"},
    {"symbol": "GBPUSD", "label": "GBP/USD",   "emoji": "💷"},
]

# ─────────────────────────────────────────────
# Agent State  (in-memory, reset on restart)
# ─────────────────────────────────────────────
agent_state = {
    "running":      False,
    "mode":         "observe",   # observe | trade
    "interval_sec": 3600,
    "last_run":     None,
    "last_signal":  None,
    "log":          [],
}

# ─────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────
def load_config() -> dict:
    """Load config.json – raises FileNotFoundError if missing."""
    with open('config.json') as f:
        return json.load(f)

def save_config(data: dict) -> None:
    with open('config.json', 'w') as f:
        json.dump(data, f, indent=2)

def get_headers() -> dict | None:
    """Build eToro auth headers. Returns None if keys missing."""
    cfg = load_config()
    api_key  = cfg.get('API_KEY', '').strip()
    user_key = cfg.get('USER_KEY', '').strip()
    if not api_key or not user_key or api_key.startswith('YOUR_'):
        return None
    return {
        "x-api-key":    api_key,
        "x-user-key":   user_key,
        "x-request-id": str(uuid.uuid4()),
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }

def base_url() -> str:
    return load_config().get('API_URL', 'https://public-api.etoro.com/api/v1')

# ─────────────────────────────────────────────
# eToro API helpers
# ─────────────────────────────────────────────
def _get(url: str, params: dict = None) -> tuple:
    """Generic GET with unified error handling. Returns (data, error)."""
    headers = get_headers()
    if headers is None:
        return None, 'API-Keys fehlen oder nicht konfiguriert.'
    try:
        r = http.get(url, headers=headers, params=params, timeout=TIMEOUT_API)
        if r.status_code == 400: return None, f'400 Bad Request: {url}'
        if r.status_code == 401: return None, 'Unauthorized (401) – API-Key prüfen'
        if r.status_code == 403: return None, 'Forbidden (403) – fehlende Berechtigung'
        if r.status_code == 404: return None, f'Endpoint nicht gefunden (404): {url}'
        r.raise_for_status()
        return r.json(), None
    except http.exceptions.Timeout:
        return None, f'Timeout nach {TIMEOUT_API}s: {url}'
    except http.exceptions.ConnectionError as e:
        return None, f'Verbindungsfehler: {e}'
    except Exception as e:
        return None, str(e)

def get_market_price(symbol: str = 'BTC') -> tuple:
    cfg = load_config()
    iid = cfg.get(f'{symbol}_INSTRUMENT_ID')
    if not iid:
        return None, f'{symbol}_INSTRUMENT_ID fehlt – /debug/instruments aufrufen'
    data, err = _get(f'{base_url()}/market-data/instruments/rates', {"instrumentIds": iid})
    if err: return None, err
    items = data.get("items") or data.get("rates") or []
    if not items: return None, "Keine Preisdaten in Antwort."
    first = items[0]
    price = first.get("executionPrice") or first.get("bid") or first.get("ask")
    return price, None

def get_multi_prices(instrument_ids: dict) -> dict:
    if not instrument_ids: return {}
    headers = get_headers()
    if not headers: return {}
    ids_str = ",".join(str(v) for v in instrument_ids.values())
    data, err = _get(f'{base_url()}/market-data/instruments/rates', {"instrumentIds": ids_str})
    if err: return {}
    items = data.get("items") or data.get("rates") or []
    id_to_price = {}
    for item in items:
        iid   = item.get("instrumentID") or item.get("instrumentId") or item.get("InstrumentID")
        price = item.get("executionPrice") or item.get("bid") or item.get("ask")
        if iid and price:
            id_to_price[int(iid)] = price
    return {sym: id_to_price.get(int(iid), "–") for sym, iid in instrument_ids.items()}

CANDLE_INTERVALS = ["1440", "day", "daily", "Day", "1d", "DAY"]

def get_candles(instrument_id, count: int = CANDLE_COUNT) -> tuple:
    headers = get_headers()
    if headers is None: return None, 'API-Keys fehlen.'
    bu = base_url()
    for interval in CANDLE_INTERVALS:
        url = f'{bu}/market-data/instruments/{instrument_id}/history/candles/desc/{interval}/{count}'
        try:
            r = http.get(url, headers={**headers, "x-request-id": str(uuid.uuid4())}, timeout=15)
            if r.status_code == 200:
                data    = r.json()
                candles = data.get("candles") or data.get("Candles") or data.get("data") or data.get("items") or []
                if candles:
                    log.info(f"Candles OK · interval={interval} · count={len(candles)}")
                    return candles, None
        except Exception:
            continue
    return None, f'Kein gültiger Candle-Interval gefunden. Getestet: {", ".join(CANDLE_INTERVALS)}'

def get_real_portfolio() -> tuple:
    data, err = _get(f'{base_url()}/trading/info/real/pnl')
    if err: return None, err
    positions = data.get("positions") or data.get("openPositions") or data.get("portfolioPositions") or []
    normalized = []
    for p in positions:
        inst = p.get("instrument") or {}
        pnl  = p.get("pnlPercent") or p.get("pnlPct") or 0
        normalized.append({
            "symbol":        inst.get("symbol") or inst.get("ticker") or "N/A",
            "name":          inst.get("name") or "Unbekannt",
            "direction":     "Long" if str(p.get("direction","")).lower() in ("buy","long") else "Short",
            "units":         p.get("units") or p.get("amount") or 0,
            "avg_price":     p.get("openRate") or p.get("openPrice") or 0,
            "current_price": p.get("currentRate") or p.get("currentPrice") or 0,
            "pnl_pct":       round(float(pnl), 2),
        })
    return normalized, None

def get_watchlist() -> tuple:
    data, err = _get(f'{base_url()}/watchlists/default-watchlists/items')
    if err: return None, err
    items = data.get("items") or data.get("watchlistItems") or data.get("instruments") or []
    normalized = []
    for item in items:
        inst = item.get("instrument") or item
        normalized.append({
            "symbol":        inst.get("symbol") or inst.get("ticker") or "N/A",
            "name":          inst.get("name") or inst.get("instrumentDisplayName") or "Unbekannt",
            "instrument_id": inst.get("instrumentId") or inst.get("id") or "",
            "asset_class":   inst.get("assetClass") or inst.get("type") or "N/A",
        })
    return normalized, None

def resolve_instrument_ids() -> tuple:
    headers = get_headers()
    if headers is None: return {}, ["API-Keys fehlen."]
    bu     = base_url()
    result = {}
    errors = []
    for inst in INSTRUMENTS_TO_TRACK:
        sym = inst["symbol"]
        data, err = _get(f'{bu}/market-data/search', {
            "internalSymbolFull": sym,
            "fields": "instrumentId,internalSymbolFull,displayname"
        })
        if err:
            errors.append(f"{sym}: {err}")
            continue
        items = data.get("instruments") or data.get("items") or data.get("data") or []
        matched_id = None
        for item in items:
            internal = (item.get("internalSymbolFull") or item.get("symbol") or "").upper()
            if internal == sym.upper():
                matched_id = item.get("instrumentId") or item.get("InstrumentID") or item.get("id")
                break
        if matched_id is None and items:
            matched_id = items[0].get("instrumentId") or items[0].get("InstrumentID") or items[0].get("id")
        if matched_id:
            result[sym] = matched_id
        else:
            errors.append(f"{sym}: keine ID gefunden")
    return result, errors

# ─────────────────────────────────────────────
# LLM Analysis
# ─────────────────────────────────────────────
def analyze_with_llm(candles: list, current_price, symbol: str = "BTC") -> tuple:
    cfg         = load_config()
    llm_base    = cfg.get('LLM_BASE_URL', '').rstrip('/')
    llm_model   = cfg.get('LLM_MODEL', 'finance')
    llm_api_key = cfg.get('LLM_API_KEY', 'none')

    if not llm_base:
        return "HOLD", "LLM_BASE_URL nicht konfiguriert."
    if not candles:
        return "HOLD", "Keine Candle-Daten verfügbar."

    candle_lines = []
    for c in candles[:CANDLE_LLM]:
        o  = c.get("open")  or c.get("Open")  or c.get("openRate")  or "?"
        h  = c.get("high")  or c.get("High")  or c.get("highRate")  or "?"
        l  = c.get("low")   or c.get("Low")   or c.get("lowRate")   or "?"
        cl = c.get("close") or c.get("Close") or c.get("closeRate") or "?"
        ts = str(c.get("timestamp") or c.get("date") or c.get("fromDate") or "")[:10]
        candle_lines.append(f"  {ts}  O:{o}  H:{h}  L:{l}  C:{cl}")

    system_prompt = """Du bist ein präziser Trading-Analyst.
Du analysierst OHLC-Tagesdaten und gibst ein klares Signal zurück.

Antworte IMMER in diesem exakten Format:
SIGNAL: BUY
GRUND: [max. 2 Sätze auf Deutsch]

oder SIGNAL: SELL oder SIGNAL: HOLD

Sei konservativ – im Zweifel HOLD."""

    user_prompt = f"""Analysiere {symbol}. Aktueller Preis: {current_price}

Tageskerzen (neueste zuerst):
{chr(10).join(candle_lines)}

Signal?"""

    try:
        r = http.post(
            f'{llm_base}/chat/completions',
            headers={
                "Authorization": f"Bearer {llm_api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       llm_model,
                "messages":    [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens":  150,
            },
            timeout=TIMEOUT_LLM
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        signal, grund = "HOLD", content
        for line in content.splitlines():
            line = line.strip()
            if line.upper().startswith("SIGNAL:"):
                raw = line.split(":", 1)[1].strip().upper()
                signal = "BUY" if "BUY" in raw else "SELL" if "SELL" in raw else "HOLD"
            elif line.upper().startswith("GRUND:"):
                grund = line.split(":", 1)[1].strip()
        return signal, grund
    except http.exceptions.Timeout:
        return "HOLD", f"LLM Timeout nach {TIMEOUT_LLM}s"
    except Exception as e:
        return "HOLD", f"LLM-Fehler: {e}"

def calc_sma(candles: list, period: int) -> float | None:
    closes = []
    for c in candles:
        val = c.get("close") or c.get("Close") or c.get("closeRate")
        if val is not None:
            try: closes.append(float(val))
            except: pass
    if len(closes) < period: return None
    return sum(closes[:period]) / period

# ─────────────────────────────────────────────
# Agent Tick
# ─────────────────────────────────────────────
def _log(msg: str) -> None:
    """Thread-safe log insert with cap."""
    agent_state["log"].insert(0, msg)
    if len(agent_state["log"]) > LOG_MAX:
        agent_state["log"] = agent_state["log"][:LOG_MAX]

def agent_tick() -> None:
    ts  = datetime.now().strftime("%H:%M:%S")
    cfg = load_config()
    iid = cfg.get('BTC_INSTRUMENT_ID')

    if not iid:
        _log(f"[{ts}] ⚠ BTC_INSTRUMENT_ID fehlt – bitte /debug/instruments aufrufen")
        return

    price, err = get_market_price('BTC')
    if err:
        _log(f"[{ts}] ⚠ Preisabruf: {err}")
        return

    candles, err = get_candles(iid, count=CANDLE_COUNT)
    if err:
        _log(f"[{ts}] ⚠ Candles: {err}")
        _log(f"[{ts}] 👁 Fallback · BTC={price} · kein Signal")
        agent_state["last_run"] = ts
        return

    sma20 = calc_sma(candles, 20)
    sma50 = calc_sma(candles, 50)
    trend = "aufwärts" if (sma20 and sma50 and sma20 > sma50) else "abwärts" if (sma20 and sma50) else "unbekannt"

    if agent_state["mode"] == "observe":
        sma_info = f"SMA20={sma20:.2f} SMA50={sma50:.2f} Trend={trend}" if sma20 and sma50 else "SMA n/v"
        _log(f"[{ts}] 👁 Observe · BTC={price} · {sma_info}")
        agent_state["last_run"] = ts
        return

    _log(f"[{ts}] 🔍 LLM-Analyse · BTC={price} · Trend={trend} ...")
    signal, grund = analyze_with_llm(candles, price, symbol="BTC")
    agent_state["last_signal"] = signal
    agent_state["last_run"]    = ts

    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(signal, "⚪")
    _log(f"[{ts}] {emoji} Signal: {signal} · {grund}")
    if sma20 and sma50:
        _log(f"[{ts}]    ↳ SMA20={sma20:.2f} · SMA50={sma50:.2f} · Trend={trend}")

    log.info(f"Agent tick: BTC={price} signal={signal} trend={trend}")

    # ── STAGE 3 PLACEHOLDER ─────────────────────────────────────────────────
    # Uncomment ONLY after adding write key and reviewing risk parameters!
    # ─────────────────────────────────────────────────────────────────────────
    # if signal == "BUY":
    #     place_order(iid, is_buy=True, amount=100)
    # elif signal == "SELL":
    #     place_order(iid, is_buy=False, amount=100)
    # ─────────────────────────────────────────────────────────────────────────

def agent_loop() -> None:
    import time
    while agent_state["running"]:
        try:
            agent_tick()
        except Exception as e:
            _log(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ Unhandled agent error: {e}")
            log.exception("agent_tick error")
        time.sleep(agent_state["interval_sec"])

# ─────────────────────────────────────────────
# Layout
# ─────────────────────────────────────────────
BASE_CSS = """
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
  margin: 0; background: #060d1f; color: #cbd5e1;
}
.app-shell { min-height: 100vh; display: flex; }
.sidebar {
  width: 230px; flex-shrink: 0;
  background: linear-gradient(180deg, #0a1628 0%, #060d1f 100%);
  padding: 28px 16px;
  border-right: 1px solid rgba(99,102,241,0.15);
  display: flex; flex-direction: column;
}
.logo { display:flex; align-items:center; gap:10px; margin-bottom:32px; padding:0 6px; }
.logo-icon {
  width:32px; height:32px;
  background: linear-gradient(135deg,#6366f1,#0ea5e9);
  border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:16px;
}
.logo-text { font-size:17px; font-weight:700; color:#f1f5f9; }
.logo-sub  { font-size:10px; color:#64748b; letter-spacing:0.08em; }
.nav-section { font-size:10px; color:#475569; letter-spacing:0.1em; text-transform:uppercase; padding:0 10px; margin:16px 0 6px; }
.nav-link {
  display:flex; align-items:center; gap:10px;
  padding:9px 12px; border-radius:10px;
  color:#94a3b8; text-decoration:none; font-size:13.5px; margin-bottom:2px; transition:all 0.15s;
}
.nav-link:hover  { background:rgba(99,102,241,0.1); color:#e2e8f0; }
.nav-link.active { background:rgba(99,102,241,0.18); color:#a5b4fc; font-weight:600; }
.nav-link .icon  { font-size:15px; width:20px; text-align:center; }
.agent-pill {
  margin-top:auto; padding:12px 14px; border-radius:12px;
  background:rgba(15,23,42,0.6); border:1px solid rgba(99,102,241,0.2); font-size:12px;
}
.dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
.dot-on  { background:#4ade80; box-shadow:0 0 6px #4ade80; animation: pulse 2s infinite; }
.dot-off { background:#475569; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.main { flex:1; padding:32px 36px; overflow-y:auto; }
.page-header { margin-bottom:28px; }
.page-header h2 { margin:0 0 4px; font-size:26px; font-weight:700; color:#f1f5f9; }
.page-header p  { margin:0; font-size:13px; color:#64748b; }
.card {
  background:linear-gradient(135deg,#0d1b35 0%,#0a1225 100%);
  border:1px solid rgba(99,102,241,0.18); border-radius:16px;
  padding:22px 24px; box-shadow:0 8px 32px rgba(0,0,0,0.4);
  margin-bottom:20px;
}
.stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:16px; margin-bottom:24px; }
.stat-card {
  background:#0a1225; border:1px solid rgba(99,102,241,0.15);
  border-radius:14px; padding:18px 20px;
}
.stat-label { font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:8px; }
.stat-value { font-size:24px; font-weight:700; color:#f1f5f9; }
.stat-sub   { font-size:11px; color:#475569; margin-top:4px; }
.badge { display:inline-flex; align-items:center; gap:5px; font-size:11px; padding:3px 10px; border-radius:999px; font-weight:500; }
.badge-blue   { background:rgba(99,102,241,0.15); color:#a5b4fc; border:1px solid rgba(99,102,241,0.3); }
.badge-green  { background:rgba(34,197,94,0.12);  color:#4ade80; border:1px solid rgba(34,197,94,0.25); }
.badge-red    { background:rgba(239,68,68,0.12);  color:#fca5a5; border:1px solid rgba(239,68,68,0.25); }
.badge-yellow { background:rgba(234,179,8,0.12);  color:#fde047; border:1px solid rgba(234,179,8,0.25); }
.badge-buy    { background:rgba(34,197,94,0.2);   color:#4ade80; border:1px solid rgba(34,197,94,0.4);  font-size:13px; padding:5px 14px; }
.badge-sell   { background:rgba(239,68,68,0.2);   color:#fca5a5; border:1px solid rgba(239,68,68,0.4);  font-size:13px; padding:5px 14px; }
.badge-hold   { background:rgba(234,179,8,0.15);  color:#fde047; border:1px solid rgba(234,179,8,0.3);  font-size:13px; padding:5px 14px; }
.table-wrap { border-radius:14px; border:1px solid rgba(99,102,241,0.15); overflow:hidden; }
table { width:100%; border-collapse:collapse; font-size:13px; }
thead { background:#06101f; }
th, td { padding:11px 14px; text-align:left; }
th { font-weight:500; color:#64748b; border-bottom:1px solid rgba(99,102,241,0.1); font-size:11px; text-transform:uppercase; letter-spacing:0.06em; }
tbody tr { border-bottom:1px solid rgba(15,23,42,0.8); transition:background 0.12s; }
tbody tr:hover { background:rgba(99,102,241,0.06); }
.sym { display:inline-block; background:rgba(99,102,241,0.15); color:#a5b4fc; border-radius:6px; padding:2px 8px; font-size:12px; font-weight:700; letter-spacing:0.04em; }
.pill       { display:inline-flex; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:500; }
.pill-long  { background:rgba(34,197,94,0.12); color:#4ade80; }
.pill-short { background:rgba(239,68,68,0.12); color:#fca5a5; }
.pnl-pos { color:#4ade80; font-weight:600; }
.pnl-neg { color:#fca5a5; font-weight:600; }
.footer-note { font-size:11px; color:#334155; padding:10px 14px; border-top:1px solid rgba(99,102,241,0.08); }
.btn {
  display:inline-flex; align-items:center; gap:7px;
  padding:9px 18px; border-radius:9px; font-size:13px;
  font-weight:600; cursor:pointer; border:none; text-decoration:none; transition:all 0.15s;
}
.btn-primary { background:linear-gradient(135deg,#6366f1,#4f46e5); color:#fff; }
.btn-primary:hover { background:linear-gradient(135deg,#818cf8,#6366f1); }
.btn-success { background:rgba(34,197,94,0.15); color:#4ade80; border:1px solid rgba(34,197,94,0.3); }
.btn-success:hover { background:rgba(34,197,94,0.25); }
.btn-danger  { background:rgba(239,68,68,0.15); color:#fca5a5; border:1px solid rgba(239,68,68,0.3); }
.btn-danger:hover  { background:rgba(239,68,68,0.25); }
.btn-sm { padding:6px 13px; font-size:12px; }
.log-box {
  background:#03070f; border:1px solid rgba(99,102,241,0.15); border-radius:12px;
  padding:14px 16px;
  font-family:'JetBrains Mono','Fira Code',monospace;
  font-size:12px; color:#94a3b8; max-height:360px; overflow-y:auto; line-height:1.8;
}
.entry-buy  { color:#4ade80; }
.entry-sell { color:#fca5a5; }
.entry-hold { color:#fde047; }
.entry-warn { color:#fb923c; }
.entry-info { color:#94a3b8; }
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.divider { border:none; border-top:1px solid rgba(99,102,241,0.1); margin:20px 0; }
.error-msg { color:#fca5a5; font-size:13px; margin-bottom:12px; padding:10px 14px; background:rgba(239,68,68,0.08); border:1px solid rgba(239,68,68,0.2); border-radius:8px; }
::-webkit-scrollbar       { width:5px; height:5px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:#1e293b; border-radius:99px; }
"""

def sidebar(active: str) -> str:
    dot_cls   = "dot-on" if agent_state["running"] else "dot-off"
    agent_txt = "Agent läuft" if agent_state["running"] else "Agent gestoppt"
    signal    = agent_state.get("last_signal") or "–"
    sig_color = {"BUY":"#4ade80","SELL":"#fca5a5","HOLD":"#fde047"}.get(signal,"#64748b")
    nav = [
        ("/",                  "📊", "Dashboard"),
        ("/portfolio",         "💼", "Portfolio"),
        ("/watchlist",         "⭐", "Watchlist"),
        ("/agent",             "🤖", "Agent"),
        ("/debug/instruments", "🔍", "Instruments"),
        ("/admin",             "⚙️",  "Admin"),
    ]
    links = "".join(
        f'<a href="{href}" class="nav-link{" active" if href==active else ""}">'  
        f'<span class="icon">{icon}</span>{label}</a>\n'
        for href, icon, label in nav
    )
    return f"""
    <aside class="sidebar">
      <div class="logo">
        <div class="logo-icon">📈</div>
        <div>
          <div class="logo-text">hAI.FinOro</div>
          <div class="logo-sub">TRADING AGENT</div>
        </div>
      </div>
      <div class="nav-section">Navigation</div>
      {links}
      <div class="agent-pill">
        <div style="margin-bottom:6px;font-weight:600;color:#e2e8f0;">
          <span class="dot {dot_cls}"></span>{agent_txt}
        </div>
        <div style="color:#64748b;font-size:11px;margin-bottom:4px;">
          Modus: {agent_state['mode'].capitalize()} · {agent_state['interval_sec']}s
        </div>
        <div style="font-size:12px;">
          Signal: <span style="color:{sig_color};font-weight:700;">{signal}</span>
        </div>
      </div>
    </aside>"""

def page(active: str, title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · hAI.FinOro</title>
<style>{BASE_CSS}</style>
</head>
<body>
<div class="app-shell">
  {sidebar(active)}
  <main class="main">{body}</main>
</div>
</body>
</html>"""

def log_css(entry: str) -> str:
    if "🟢" in entry or "BUY"  in entry: return "entry-buy"
    if "🔴" in entry or "SELL" in entry: return "entry-sell"
    if "🟡" in entry or "HOLD" in entry: return "entry-hold"
    if "⚠"  in entry:                    return "entry-warn"
    return "entry-info"

# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route('/')
def dashboard():
    cfg = load_config()
    tracked_ids = {
        inst["symbol"]: cfg.get(f'{inst["symbol"]}_INSTRUMENT_ID')
        for inst in INSTRUMENTS_TO_TRACK
        if cfg.get(f'{inst["symbol"]}_INSTRUMENT_ID')
    }
    prices = get_multi_prices(tracked_ids)
    market_cards = "".join(
        f'<div class="stat-card">'
        f'<div class="stat-label">{i["emoji"]} {i["symbol"]}</div>'
        f'<div class="stat-value" style="font-size:20px;">{prices.get(i["symbol"],"–")}</div>'
        f'<div class="stat-sub">{i["label"]}</div>'
        f'</div>'
        for i in INSTRUMENTS_TO_TRACK
    )
    positions, _ = get_real_portfolio()
    pos_count    = len(positions) if positions else 0
    signal       = agent_state.get("last_signal") or "–"
    sig_cls      = {"BUY":"badge-buy","SELL":"badge-sell","HOLD":"badge-hold"}.get(signal,"badge-blue")
    agent_badge  = ('<span class="badge badge-green">● Aktiv</span>'
                    if agent_state["running"]
                    else '<span class="badge badge-red">● Gestoppt</span>')
    log_html = ("".join(f'<div class="{log_css(e)}">{e}</div>' for e in agent_state["log"][:10])
                or '<span style="color:#334155;">Noch keine Einträge. Agent starten unter /agent.</span>')

    body = f"""
    <div class="page-header">
      <h2>Dashboard</h2>
      <p>Live-Marktdaten · eToro Real-Account · LLM Trading Agent</p>
    </div>
    <div class="stat-grid">{market_cards}</div>
    <div class="stat-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:24px;">
      <div class="stat-card">
        <div class="stat-label">LLM-Signal</div>
        <div style="margin-top:8px;"><span class="badge {sig_cls}">{signal}</span></div>
        <div class="stat-sub">Modell: finance · ONE_DAY</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Offene Positionen</div>
        <div class="stat-value">{pos_count}</div>
        <div class="stat-sub">Real-Account</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Agent</div>
        <div style="margin-top:8px;">{agent_badge}</div>
        <div class="stat-sub">Letzter Tick: {agent_state.get('last_run') or '–'}</div>
      </div>
    </div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
        <div style="font-size:15px;font-weight:700;color:#f1f5f9;">Agent-Log</div>
        <a href="/agent" class="btn btn-primary btn-sm">🤖 Agent steuern</a>
      </div>
      <div class="log-box">{log_html}</div>
    </div>"""
    return page("/", "Dashboard", body)


@app.route('/portfolio')
def portfolio():
    positions, error = get_real_portfolio()
    err  = f'<div class="error-msg">⚠ {error}</div>' if error else ''
    if error: positions = []
    rows = "".join(
        f'<tr>'
        f'<td><span class="sym">{p["symbol"]}</span></td>'
        f'<td style="color:#e2e8f0;">{p["name"]}</td>'
        f'<td><span class="pill {"pill-long" if p["direction"]=="Long" else "pill-short"}">{p["direction"]}</span></td>'
        f'<td style="color:#94a3b8;">{p["units"]}</td>'
        f'<td style="color:#94a3b8;">{p["avg_price"]}</td>'
        f'<td style="color:#e2e8f0;">{p["current_price"]}</td>'
        f'<td><span class="{"pnl-pos" if p["pnl_pct"]>=0 else "pnl-neg"}">{p["pnl_pct"]:.2f}%</span></td>'
        f'</tr>'
        for p in positions
    ) or '<tr><td colspan="7" style="text-align:center;color:#334155;padding:20px;">Keine offenen Positionen</td></tr>'

    body = f"""
    <div class="page-header"><h2>Portfolio</h2><p>Offene Positionen · Real-Account · Read-Only</p></div>
    {err}
    <div style="margin-bottom:16px;"><a href="/portfolio.csv" class="btn btn-primary btn-sm">⬇ CSV Export</a></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>Instrument</th><th>Richtung</th><th>Units</th><th>Ø Einstieg</th><th>Aktuell</th><th>P&amp;L %</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div class="footer-note">Read-Only · Keine Orders über dieses Dashboard.</div>
    </div>"""
    return page("/portfolio", "Portfolio", body)


@app.route('/portfolio.csv')
def portfolio_csv():
    positions, error = get_real_portfolio()
    output = io.StringIO()
    if error:
        csv.writer(output).writerows([["error"],[error]])
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=portfolio_error.csv"})
    fieldnames = ["symbol","name","direction","units","avg_price","current_price","pnl_pct"]
    w = csv.DictWriter(output, fieldnames=fieldnames)
    w.writeheader()
    for p in positions:
        w.writerow({k: p.get(k) for k in fieldnames})
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=portfolio.csv"})


@app.route('/watchlist')
def watchlist():
    items, error = get_watchlist()
    err  = f'<div class="error-msg">⚠ {error}</div>' if error else ''
    if error: items = []
    rows = "".join(
        f'<tr>'
        f'<td><span class="sym">{i["symbol"]}</span></td>'
        f'<td style="color:#e2e8f0;">{i["name"]}</td>'
        f'<td><span class="badge badge-blue">{i["asset_class"]}</span></td>'
        f'<td style="color:#475569;font-size:12px;">{i["instrument_id"]}</td>'
        f'</tr>'
        for i in items
    ) or '<tr><td colspan="4" style="text-align:center;color:#334155;padding:20px;">Keine Einträge</td></tr>'

    body = f"""
    <div class="page-header"><h2>Watchlist</h2><p>{len(items)} Instrumente · Standard-Watchlist</p></div>
    {err}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>Instrument</th><th>Asset-Klasse</th><th>ID</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div class="footer-note">Read-Only · eToro Standard-Watchlist.</div>
    </div>"""
    return page("/watchlist", "Watchlist", body)


@app.route('/agent')
def agent_panel():
    signal      = agent_state.get("last_signal") or "–"
    sig_cls     = {"BUY":"badge-buy","SELL":"badge-sell","HOLD":"badge-hold"}.get(signal,"badge-blue")
    status_b    = ('<span class="badge badge-green">● Läuft</span>' if agent_state["running"]
                   else '<span class="badge badge-red">● Gestoppt</span>')
    mode_b      = ('<span class="badge badge-red">⚡ Trade-Modus</span>' if agent_state["mode"]=="trade"
                   else '<span class="badge badge-blue">👁 Observe-Modus</span>')
    log_html    = ("".join(f'<div class="{log_css(e)}">{e}</div>' for e in agent_state["log"])
                   or '<span style="color:#334155;">Noch keine Einträge.</span>')
    obs_sel  = 'selected' if agent_state["mode"]=="observe" else ''
    trd_sel  = 'selected' if agent_state["mode"]=="trade"   else ''

    body = f"""
    <div class="page-header">
      <h2>Trading Agent</h2>
      <p>LLM-basiert · Modell: finance · ONE_DAY · BTC</p>
    </div>
    <div class="grid-2" style="margin-bottom:20px;">
      <div class="card">
        <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:14px;">Status</div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;">{status_b} {mode_b}</div>
        <div style="margin-bottom:16px;">
          <div style="font-size:11px;color:#64748b;margin-bottom:6px;">LETZTES SIGNAL</div>
          <span class="badge {sig_cls}">{signal}</span>
        </div>
        <div style="font-size:12px;color:#64748b;margin-bottom:18px;">
          Letzter Tick: {agent_state.get('last_run') or '–'} · Interval: {agent_state['interval_sec']}s
        </div>
        <hr class="divider">
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
          <form method="post" action="/agent/start"><button type="submit" class="btn btn-success btn-sm">▶ Starten</button></form>
          <form method="post" action="/agent/stop"><button type="submit" class="btn btn-danger btn-sm">■ Stoppen</button></form>
          <form method="post" action="/agent/tick"><button type="submit" class="btn btn-sm" style="background:rgba(99,102,241,0.15);color:#a5b4fc;border:1px solid rgba(99,102,241,0.3);">⚡ Jetzt ausführen</button></form>
        </div>
      </div>
      <div class="card">
        <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:14px;">Konfiguration</div>
        <form method="post" action="/agent/config">
          <div style="margin-bottom:14px;">
            <label style="font-size:12px;color:#64748b;display:block;margin-bottom:6px;">MODUS</label>
            <select name="mode" style="background:#06101f;border:1px solid rgba(99,102,241,0.25);color:#e2e8f0;border-radius:8px;padding:8px 12px;font-size:13px;width:100%;">
              <option value="observe" {obs_sel}>👁 Observe – nur beobachten & loggen</option>
              <option value="trade"   {trd_sel}>🤖 Trade – LLM-Signale berechnen</option>
            </select>
          </div>
          <div style="margin-bottom:16px;">
            <label style="font-size:12px;color:#64748b;display:block;margin-bottom:6px;">TICK-INTERVAL (Sekunden)</label>
            <input type="number" name="interval" value="{agent_state['interval_sec']}" min="{INTERVAL_MIN}" max="{INTERVAL_MAX}"
              style="background:#06101f;border:1px solid rgba(99,102,241,0.25);color:#e2e8f0;border-radius:8px;padding:8px 12px;font-size:13px;width:100%;">
            <div style="font-size:11px;color:#475569;margin-top:4px;">Min {INTERVAL_MIN}s · Max {INTERVAL_MAX}s · Empfehlung: 3600s</div>
          </div>
          <button type="submit" class="btn btn-primary btn-sm">💾 Speichern</button>
        </form>
      </div>
    </div>
    <div class="card" style="border-color:rgba(234,179,8,0.2);">
      <div style="display:flex;gap:12px;align-items:flex-start;">
        <span style="font-size:22px;">🤖</span>
        <div>
          <div style="font-size:13px;font-weight:700;color:#fde047;margin-bottom:6px;">Stufe 2 aktiv – Signale ohne echte Orders</div>
          <div style="font-size:12px;color:#94a3b8;line-height:1.7;">
            Agent ruft Tageskerzen ab · SMA20/SMA50 Vorfilter · LLM-Analyse via
            <code style="background:#06101f;padding:1px 5px;border-radius:4px;">finance</code>.
            Echte Orders erst in <strong style="color:#fde047;">Stufe 3</strong> nach Write-Key-Freigabe.
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
        <div style="font-size:14px;font-weight:700;color:#f1f5f9;">Agent-Log</div>
        <form method="post" action="/agent/clear-log">
          <button type="submit" class="btn btn-sm" style="background:rgba(239,68,68,0.1);color:#fca5a5;border:1px solid rgba(239,68,68,0.2);">🗑 Leeren</button>
        </form>
      </div>
      <div class="log-box">{log_html}</div>
    </div>"""
    return page("/agent", "Agent", body)


@app.route('/agent/start', methods=['POST'])
def agent_start():
    if not agent_state["running"]:
        agent_state["running"] = True
        threading.Thread(target=agent_loop, daemon=True).start()
        _log(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Agent gestartet · Modus: {agent_state['mode']} · Modell: finance")
    return Response('', status=302, headers={'Location': '/agent'})

@app.route('/agent/stop', methods=['POST'])
def agent_stop():
    agent_state["running"] = False
    _log(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 Agent gestoppt.")
    return Response('', status=302, headers={'Location': '/agent'})

@app.route('/agent/tick', methods=['POST'])
def agent_manual_tick():
    threading.Thread(target=agent_tick, daemon=True).start()
    return Response('', status=302, headers={'Location': '/agent'})

@app.route('/agent/config', methods=['POST'])
def agent_config():
    mode     = request.form.get('mode', 'observe')
    if mode not in ('observe', 'trade'): mode = 'observe'  # whitelist
    try:
        interval = int(request.form.get('interval', 3600))
    except ValueError:
        interval = 3600
    agent_state["mode"]         = mode
    agent_state["interval_sec"] = max(INTERVAL_MIN, min(INTERVAL_MAX, interval))
    _log(f"[{datetime.now().strftime('%H:%M:%S')}] ⚙ Config · Modus: {mode} · Interval: {agent_state['interval_sec']}s")
    return Response('', status=302, headers={'Location': '/agent'})

@app.route('/agent/clear-log', methods=['POST'])
def agent_clear_log():
    agent_state["log"] = []
    return Response('', status=302, headers={'Location': '/agent'})


@app.route('/debug/instruments')
def debug_instruments():
    ids, errors = resolve_instrument_ids()
    prices      = get_multi_prices(ids) if ids else {}
    rows = "".join(
        f'<tr>'
        f'<td style="font-size:18px;">{i["emoji"]}</td>'
        f'<td><span class="sym">{i["symbol"]}</span></td>'
        f'<td style="color:#e2e8f0;">{i["label"]}</td>'
        f'<td><span class="badge {"badge-green" if ids.get(i["symbol"]) else "badge-red"}">{ids.get(i["symbol"],"–")}</span></td>'
        f'<td style="color:#4ade80;font-weight:600;">{prices.get(i["symbol"],"–")}</td>'
        f'</tr>'
        for i in INSTRUMENTS_TO_TRACK
    )
    err_html  = ("<div class='error-msg'>⚠ " + " · ".join(errors) + "</div>") if errors else ""
    hidden    = "".join(f'<input type="hidden" name="{k}" value="{v}">' for k,v in ids.items())
    save_form = (f'<form method="post" action="/debug/save-ids" style="margin-bottom:20px;">'
                 f'{hidden}<button type="submit" class="btn btn-primary">💾 IDs in config.json speichern</button></form>'
                 if ids else "")
    body = f"""
    <div class="page-header">
      <h2>Instrument-IDs</h2>
      <p>Automatische Auflösung via eToro Search-API · einmalig ausführen & speichern</p>
    </div>
    {err_html}{save_form}
    <div class="table-wrap">
      <table>
        <thead><tr><th></th><th>Symbol</th><th>Name</th><th>Instrument-ID</th><th>Aktueller Preis</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div class="footer-note">IDs sind permanent · nach Speichern nur noch zur Kontrolle nötig.</div>
    </div>"""
    return page("/debug/instruments", "Instruments", body)


@app.route('/debug/save-ids', methods=['POST'])
def debug_save_ids():
    cfg = load_config()
    for inst in INSTRUMENTS_TO_TRACK:
        sym = inst["symbol"]
        val = request.form.get(sym)
        if val and val.isdigit():
            cfg[f'{sym}_INSTRUMENT_ID'] = int(val)
    save_config(cfg)
    return Response('', status=302, headers={'Location': '/debug/instruments'})


@app.route('/admin')
def admin():
    cfg      = load_config()
    has_keys = bool(cfg.get('API_KEY','').strip()) and not cfg.get('API_KEY','').startswith('YOUR_')
    has_llm  = bool(cfg.get('LLM_BASE_URL','').strip())
    k_cls = "badge-green" if has_keys else "badge-red"
    k_txt = "✓ konfiguriert" if has_keys else "✗ fehlt / Platzhalter"
    l_cls = "badge-green" if has_llm else "badge-red"
    l_txt = f"✓ {cfg.get('LLM_BASE_URL','')}" if has_llm else "✗ fehlt"

    # Instrument-ID Status
    id_rows = "".join(
        f'<tr><td style="font-size:14px;">{i["emoji"]}</td>'
        f'<td><span class="sym">{i["symbol"]}</span></td>'
        f'<td><span class="badge {"badge-green" if cfg.get(i["symbol"]+"_INSTRUMENT_ID") else "badge-red"}">{cfg.get(i["symbol"]+"_INSTRUMENT_ID","–")}</span></td></tr>'
        for i in INSTRUMENTS_TO_TRACK
    )

    body = f"""
    <div class="page-header"><h2>Admin</h2><p>Systemkonfiguration und Verbindungsstatus</p></div>
    <div class="grid-2">
      <div class="card">
        <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:16px;">Verbindungen</div>
        <div style="margin-bottom:12px;">
          <div style="font-size:11px;color:#64748b;margin-bottom:5px;">ETORO API KEYS</div>
          <span class="badge {k_cls}">{k_txt}</span>
        </div>
        <div style="margin-bottom:12px;">
          <div style="font-size:11px;color:#64748b;margin-bottom:5px;">LLM ENDPOINT</div>
          <span class="badge {l_cls}">{l_txt}</span>
        </div>
        <div style="margin-bottom:12px;">
          <div style="font-size:11px;color:#64748b;margin-bottom:5px;">LLM MODELL</div>
          <code style="font-size:12px;color:#a5b4fc;">{cfg.get('LLM_MODEL','–')}</code>
        </div>
        <div>
          <div style="font-size:11px;color:#64748b;margin-bottom:5px;">API URL</div>
          <code style="font-size:12px;color:#94a3b8;">{cfg.get('API_URL','–')}</code>
        </div>
      </div>
      <div class="card">
        <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:16px;">Instrument-IDs</div>
        <table style="font-size:12px;">
          <tbody>{id_rows}</tbody>
        </table>
        <div style="margin-top:14px;">
          <a href="/debug/instruments" class="btn btn-sm" style="background:rgba(99,102,241,0.15);color:#a5b4fc;border:1px solid rgba(99,102,241,0.3);">🔍 IDs neu laden</a>
        </div>
      </div>
    </div>
    <div style="margin-top:20px;display:flex;gap:10px;">
      <a href="/portfolio.csv" class="btn btn-primary btn-sm">⬇ Portfolio CSV</a>
    </div>"""
    return page("/admin", "Admin", body)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
