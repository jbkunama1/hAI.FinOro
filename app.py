#!/usr/bin/env python3
from __future__ import annotations
"""
hAI.FinOro – KI-gestützter Trading-Agent
Mit Passwortschutz, SQLite-Tracking, Handelszeit und Chart-Ansichten.
"""

import json
import logging
import os
import uuid
import sqlite3
from collections import deque
from datetime import datetime, time
from functools import wraps
from typing import Optional, Tuple, Dict, List

from zoneinfo import ZoneInfo
from flask import Flask, Response, request, session, redirect, url_for
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Logging ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konstanten ───────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
TIMEOUT_API = 10
MAX_LOG     = 100
VALID_MODES = {"observe", "trade"}
TITLE       = "hAI.FinOro"

# ── HTTP-Session mit Retry ───────────────────────────────────────────────────────
http = requests.Session()
http.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])
    ),
)

# ── In-Memory-Log ────────────────────────────────────────────────────────────────
_log_buf: deque[str] = deque(maxlen=MAX_LOG)


def _log(msg: str) -> None:
    _log_buf.appendleft(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
    log.info(msg)


# ── Instrumente ──────────────────────────────────────────────────────────────────
INSTRUMENTS_TO_TRACK: List[Dict[str, str]] = [
    {"symbol": "BTC",  "label": "Bitcoin",         "emoji": "₿",  "search": "Bitcoin",  "cfg_key": "BTC_INSTRUMENT_ID"},
    {"symbol": "ETH",  "label": "Ethereum",        "emoji": "Ξ",  "search": "Ethereum", "cfg_key": "ETH_INSTRUMENT_ID"},
    {"symbol": "GOLD", "label": "Gold (Spot)",     "emoji": "🥇", "search": "Gold",     "cfg_key": "GOLD_INSTRUMENT_ID"},
    {"symbol": "OIL",  "label": "Crude Oil (WTI)", "emoji": "🛢️", "search": "Oil WTI",  "cfg_key": "OIL_INSTRUMENT_ID"},
    {"symbol": "EUR",  "label": "EUR/USD",         "emoji": "€",  "search": "EURUSD",   "cfg_key": "EURUSD_INSTRUMENT_ID"},
    {"symbol": "GBP",  "label": "GBP/USD",         "emoji": "£",  "search": "GBPUSD",   "cfg_key": "GBPUSD_INSTRUMENT_ID"},
]

# ── Default-Config ───────────────────────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    "API_KEY":              "",
    "USER_KEY":             "",
    "SECRET_KEY":           "change-me",
    "API_URL":              "https://public-api.etoro.com/api/v1",
    "BASE_URL":             "https://api.etoro.com",
    "SANDBOX":              False,
    "LLM_BASE_URL":         "https://9router.arbeitermili.eu/v1",
    "LLM_URL":              "https://9router.arbeitermili.eu/v1",
    "LLM_MODEL":            "finance",
    "LLM_API_KEY":          "",
    "MODE":                 "observe",
    "INTERVAL":             300,
    "TRADE_AMOUNT":         0.0,
    "MARKET_TIMEZONE":      "Europe/Berlin",
    "TRADE_START":          "08:00",
    "TRADE_END":            "22:00",
    "BTC_INSTRUMENT_ID":    100134,
    "ETH_INSTRUMENT_ID":    100125,
    "GOLD_INSTRUMENT_ID":   559,
    "OIL_INSTRUMENT_ID":    784,
    "EURUSD_INSTRUMENT_ID": 1,
    "GBPUSD_INSTRUMENT_ID": 2,
    "ADMIN_PASSWORD":       "",
    "DB_PATH":              "finoro.db",
}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        updated = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = v
                updated = True
        if updated:
            save_config(data)
        return data
    except FileNotFoundError:
        _log(f"config.json nicht vorhanden – erstelle neue unter: {CONFIG_PATH}")
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()
    except json.JSONDecodeError as e:
        _log(f"config.json JSON-Fehler: {e} – verwende Defaults")
        return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        _log(f"config.json Schreibfehler: {e}")


# ── SQLite ───────────────────────────────────────────────────────────────────────
_cfg_for_db = load_config()
DB_PATH     = _cfg_for_db.get("DB_PATH", "finoro.db")


def init_db() -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    instrument_id INTEGER,
                    symbol TEXT,
                    direction TEXT,
                    amount REAL,
                    response_json TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    mode TEXT,
                    prices_json TEXT,
                    signal TEXT
                )
            """)
            conn.commit()
        _log(f"SQLite-DB initialisiert: {DB_PATH}")
    except sqlite3.Error as e:
        _log(f"SQLite-Fehler bei init_db: {e}")


def log_order(instrument_id: int, symbol: str, direction: str, amount: float, response: dict) -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO orders (ts, instrument_id, symbol, direction, amount, response_json) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.utcnow().isoformat(), instrument_id, symbol, direction.upper(), float(amount), json.dumps(response)),
            )
            conn.commit()
    except sqlite3.Error as e:
        _log(f"SQLite-Fehler beim Loggen der Order: {e}")


def log_signal(mode: str, prices: dict, signal: str) -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO signals (ts, mode, prices_json, signal) VALUES (?, ?, ?, ?)",
                (datetime.utcnow().isoformat(), mode, json.dumps(prices), signal),
            )
            conn.commit()
    except sqlite3.Error as e:
        _log(f"SQLite-Fehler beim Loggen des Signals: {e}")


# ── Flask-App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = _cfg_for_db.get("SECRET_KEY", "change-me")


# ── Auth ─────────────────────────────────────────────────────────────────────────
def is_authenticated() -> bool:
    return session.get("authenticated") is True


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if is_authenticated():
            return fn(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return wrapper


# ── API-Helpers ───────────────────────────────────────────────────────────────────
def get_headers() -> Optional[dict]:
    cfg      = load_config()
    api_key  = cfg.get("API_KEY", "").strip()
    user_key = cfg.get("USER_KEY", "").strip()
    if not api_key or not user_key:
        _log("API_KEY oder USER_KEY fehlen in config.json")
        return None
    return {
        "x-api-key":  api_key,
        "x-user-key": user_key,
        "Accept":     "application/json",
    }


def get_llm_headers() -> dict:
    cfg = load_config()
    lk  = cfg.get("LLM_API_KEY", "").strip()
    h: dict = {"Content-Type": "application/json"}
    if lk:
        h["Authorization"] = f"Bearer {lk}"
    return h


def api_url(path: str = "") -> str:
    cfg  = load_config()
    base = cfg.get("API_URL", "https://public-api.etoro.com/api/v1").rstrip("/")
    return base + path


def llm_url(path: str = "") -> str:
    cfg  = load_config()
    base = cfg.get("LLM_BASE_URL", "https://9router.arbeitermili.eu/v1").rstrip("/")
    return base + path


def api_get(path: str, params: Optional[dict] = None) -> Optional[requests.Response]:
    headers = get_headers()
    if headers is None:
        return None
    try:
        r = http.get(
            api_url(path),
            headers={**headers, "x-request-id": str(uuid.uuid4())},
            params=params or {},
            timeout=TIMEOUT_API,
        )
        if r.status_code == 401:
            _log(f"401 Unauthorized für {path} – prüfe API_KEY / USER_KEY.")
        elif r.status_code >= 400:
            _log(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r
    except requests.ConnectionError as e:
        _log(f"Verbindungsfehler: {e}")
    except requests.Timeout:
        _log("API-Timeout")
    except requests.RequestException as e:
        _log(f"HTTP-Fehler: {e}")
    return None


# ── Handelszeit ───────────────────────────────────────────────────────────────────
def parse_hhmm(s: str) -> Optional[time]:
    try:
        hh, mm = s.split(":")
        return time(hour=int(hh), minute=int(mm))
    except Exception:
        return None


def is_within_trade_window(cfg: dict) -> bool:
    tz_name = cfg.get("MARKET_TIMEZONE", "Europe/Berlin")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Berlin")
    now_local = datetime.now(tz).time()
    start = parse_hhmm(cfg.get("TRADE_START", "00:00"))
    end   = parse_hhmm(cfg.get("TRADE_END",   "23:59"))
    if not start or not end:
        _log("TRADE_START/TRADE_END ungültig – kein Handel.")
        return False
    if start <= end:
        return start <= now_local <= end
    return now_local >= start or now_local <= end


# ── Preis-Abfrage ─────────────────────────────────────────────────────────────────
def get_price(instrument_id: int) -> Optional[str]:
    r = api_get("/market-data/instruments/rates", params={"instrumentIds": instrument_id})
    if not r:
        return None
    if r.status_code != 200:
        _log(f"Rates -> HTTP {r.status_code}: {r.text[:200]}")
        return None
    try:
        data = r.json()
    except ValueError as e:
        _log(f"JSON-Fehler Rates {instrument_id}: {e}")
        return None
    rates = data.get("rates") or data.get("items") or []
    if not rates:
        return None
    entry = rates[0]
    val = entry.get("lastExecution") or entry.get("bid") or entry.get("ask")
    return str(val) if val is not None else None


def get_multi_prices(ids: dict) -> dict:
    prices = {}
    for sym, iid in ids.items():
        p = get_price(int(iid))
        if p is not None:
            prices[sym] = p
    return prices


# ── Instrument-Suche & IDs ────────────────────────────────────────────────────────
def search_instrument(query: str) -> Tuple[List[dict], Optional[str]]:
    headers = get_headers()
    if headers is None:
        return [], "API-Keys fehlen in config.json."

    results: List[dict] = []
    errors:  List[str]  = []

    endpoints = [
        (api_url("/market-data/instruments"), {"symbol": query.upper(), "limit": 10}),
        (api_url("/market-data/instruments"), {"query":  query,         "limit": 10}),
        (api_url("/instruments"),             {"q":      query,         "limit": 10}),
        (api_url("/instruments/search"),      {"query":  query}),
        (api_url(f"/instruments/{query.upper()}"), {}),
    ]

    for url, params in endpoints:
        try:
            kw: dict = dict(headers={**headers, "x-request-id": str(uuid.uuid4())}, timeout=TIMEOUT_API)
            if params:
                kw["params"] = params
            r = http.get(url, **kw)
            if r.status_code == 200:
                data  = r.json()
                items: list = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = (
                        data.get("instruments")
                        or data.get("items")
                        or data.get("data")
                        or data.get("result")
                        or []
                    )
                    if not items and data.get("instrumentId"):
                        items = [data]
                for item in items:
                    iid  = item.get("instrumentId") or item.get("InstrumentId") or item.get("id")
                    sym  = item.get("internalSymbol") or item.get("symbol") or item.get("ticker") or "?"
                    name = item.get("displayName") or item.get("displayname") or item.get("name") or "?"
                    cls  = item.get("assetClass") or item.get("instrumentType") or item.get("type") or "?"
                    if iid and not any(x["id"] == iid for x in results):
                        results.append({"id": int(iid), "symbol": sym, "name": name, "class": cls})
                if results:
                    return results[:10], None
            else:
                errors.append(f"{url} -> HTTP {r.status_code}")
        except requests.ConnectionError as e:
            errors.append(f"Verbindungsfehler: {e}")
        except requests.Timeout:
            errors.append("Timeout")
        except Exception as e:
            errors.append(str(e))

    err_detail = " · ".join(errors) if errors else "keine weiteren Details"
    return [], f'Kein Instrument für "{query}" gefunden · {err_detail}'


def resolve_instrument_ids() -> Tuple[dict, List[str]]:
    cfg    = load_config()
    ids:    Dict[str, int] = {}
    errors: List[str]      = []

    for inst in INSTRUMENTS_TO_TRACK:
        sym     = inst["symbol"]
        cfg_key = inst.get("cfg_key", f"{sym}_INSTRUMENT_ID")
        cached  = cfg.get(cfg_key)
        if cached:
            ids[sym] = int(cached)
            continue
        default_id = DEFAULT_CONFIG.get(cfg_key)
        if default_id:
            ids[sym] = int(default_id)
            cfg[cfg_key] = int(default_id)
            save_config(cfg)
            _log(f"{sym}: Default-ID {default_id} gespeichert ({cfg_key})")
            continue
        results, err = search_instrument(inst["search"])
        if err:
            errors.append(f"{sym}: {err}")
        elif results:
            ids[sym] = int(results[0]["id"])
            cfg[cfg_key] = int(results[0]["id"])
            save_config(cfg)
            _log(f"{sym}: ID {results[0]['id']} via API gefunden")
        else:
            errors.append(f"{sym}: kein Ergebnis")

    return ids, errors


# ── API-Key-Test ──────────────────────────────────────────────────────────────────
def test_api_keys(api_key: str, user_key: str, api_url_cfg: str) -> dict:
    result = {"ok": False, "messages": []}
    api_key     = api_key.strip()
    user_key    = user_key.strip()
    api_url_cfg = api_url_cfg.strip().rstrip("/") or "https://public-api.etoro.com/api/v1"

    if not api_key or not user_key:
        result["messages"].append(
            "API_KEY oder USER_KEY fehlen. Im eToro API-Portal unter Settings → Trading → "
            "API Key Management einen Key anlegen."
        )
        return result

    try:
        r = http.get(
            f"{api_url_cfg}/market-data/instruments/rates",
            headers={
                "x-api-key":      api_key,
                "x-user-key":     user_key,
                "x-request-id":   str(uuid.uuid4()),
                "Accept":         "application/json",
            },
            params={"instrumentIds": DEFAULT_CONFIG.get("BTC_INSTRUMENT_ID", 100134)},
            timeout=TIMEOUT_API,
        )
    except requests.ConnectionError as e:
        result["messages"].append(f"Verbindungsfehler: {e}")
        return result
    except requests.Timeout:
        result["messages"].append("Timeout bei der eToro API.")
        return result
    except Exception as e:
        result["messages"].append(f"Fehler: {e}")
        return result

    if r.status_code == 200:
        result["ok"] = True
        result["messages"].append("✅ API-Key-Test erfolgreich.")
        return result
    if r.status_code == 401:
        result["messages"].append("❌ 401 Unauthorized – prüfe API_KEY / USER_KEY.")
    elif r.status_code == 403:
        result["messages"].append("❌ 403 Forbidden.")
    elif r.status_code == 404:
        result["messages"].append(f"❌ 404 Not Found: {api_url_cfg}/market-data/instruments/rates")
    else:
        result["messages"].append(f"❌ HTTP {r.status_code}: {r.text[:200]}")
    return result


# ── LLM-Signal & Order ────────────────────────────────────────────────────────────
def get_llm_signal(context: dict) -> str:
    cfg   = load_config()
    model = cfg.get("LLM_MODEL", "finance")
    prompt = (
        f"Du bist ein Trading-Assistent. Analysiere:\n{json.dumps(context, indent=2)}\n"
        "Antworte mit BUY, SELL oder HOLD + kurze Begründung (max 2 Sätze)."
    )
    endpoints_to_try = [
        (llm_url("/chat/completions"), {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 150, "temperature": 0.2,
        }),
        (llm_url("/completions"), {
            "model": model, "prompt": prompt,
            "max_tokens": 150, "temperature": 0.2,
        }),
        (cfg.get("LLM_BASE_URL", "http://localhost:11434").rstrip("/") + "/api/generate", {
            "model": model, "prompt": prompt, "stream": False,
        }),
    ]
    for ep, payload in endpoints_to_try:
        try:
            r = http.post(ep, headers=get_llm_headers(), json=payload, timeout=30)
            if r.status_code == 200:
                d = r.json()
                if "choices" in d:
                    msg = d["choices"][0]
                    return (msg.get("message", {}).get("content") or msg.get("text") or "HOLD").strip()
                if "response" in d:
                    return d["response"].strip()
        except requests.ConnectionError:
            continue
        except requests.Timeout:
            _log("LLM-Timeout")
            continue
        except Exception as e:
            _log(f"LLM-Fehler: {e}")
            continue
    _log("LLM: kein Endpunkt erreichbar")
    return "HOLD — LLM nicht erreichbar"


def place_order(instrument_id: int, direction: str, amount: float, symbol: str = "") -> dict:
    headers = get_headers()
    if headers is None:
        return {"error": "API-Keys fehlen"}
    payload = {
        "instrumentId": instrument_id,
        "direction":    direction.upper(),
        "amount":       amount,
        "type":         "market",
    }
    try:
        r = http.post(
            api_url("/orders"),
            headers={**headers, "x-request-id": str(uuid.uuid4())},
            json=payload,
            timeout=TIMEOUT_API,
        )
        result = r.json() if r.status_code in (200, 201) else {"error": r.text}
        log_order(instrument_id, symbol or "?", direction, amount, result)
        return result
    except Exception as e:
        err = {"error": str(e)}
        log_order(instrument_id, symbol or "?", direction, amount, err)
        return err


def agent_tick() -> None:
    cfg  = load_config()
    mode = cfg.get("MODE", "observe")
    if mode not in VALID_MODES:
        _log(f"Ungültiger Modus {mode!r}, setze auf observe")
        mode = "observe"

    ids, errs = resolve_instrument_ids()
    for e in errs:
        _log(f"Auflösungsfehler: {e}")

    prices = get_multi_prices(ids)
    if not prices:
        _log("Keine Preise verfügbar, Tick übersprungen")
        return

    signal = get_llm_signal({"prices": prices, "mode": mode})
    _log(f"LLM-Signal: {signal}")
    log_signal(mode, prices, signal)

    if mode == "trade":
        if not is_within_trade_window(cfg):
            _log("Außerhalb der Handelszeit – keine Orders.")
            return
        if signal.startswith("BUY"):
            iid    = ids.get("BTC")
            amount = float(cfg.get("TRADE_AMOUNT", 0))
            if iid and amount > 0:
                result = place_order(iid, "buy", amount, "BTC")
                _log(f"Order-Ergebnis: {result}")


# ── STYLE ────────────────────────────────────────────────────────────────────────
STYLE = """
<style>
  :root{
    --bg:#06101f;--surface:rgba(255,255,255,0.03);--surface2:rgba(255,255,255,0.055);
    --border:rgba(99,102,241,0.18);--text:#e2e8f0;--muted:#64748b;--faint:#334155;
    --primary:#6366f1;--primary-dim:rgba(99,102,241,0.15);
    --green:#4ade80;--green-dim:rgba(74,222,128,0.12);
    --red:#f87171;--red-dim:rgba(248,113,113,0.12);
    --blue:#60a5fa;--blue-dim:rgba(96,165,250,0.12);
    --yellow:#fbbf24;--yellow-dim:rgba(251,191,36,0.12);
    --radius:10px;--radius-sm:6px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);
       display:flex;min-height:100vh;font-size:14px;line-height:1.6}
  a{color:inherit;text-decoration:none}
  .sidebar{width:220px;min-height:100vh;background:rgba(255,255,255,0.02);
            border-right:1px solid var(--border);padding:24px 0;flex-shrink:0;
            display:flex;flex-direction:column}
  .sidebar-logo{padding:0 20px 28px;border-bottom:1px solid var(--border);margin-bottom:16px}
  .sidebar-logo .logo-title{font-size:18px;font-weight:700;background:linear-gradient(135deg,#6366f1,#a855f7);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .sidebar-logo .logo-sub{font-size:11px;color:var(--muted);margin-top:2px}
  .nav-item{display:flex;align-items:center;gap:10px;padding:9px 20px;color:var(--muted);
             font-size:13px;transition:all .15s;border-left:3px solid transparent;cursor:pointer}
  .nav-item:hover{color:var(--text);background:rgba(255,255,255,0.03)}
  .nav-item.active{color:var(--text);background:var(--primary-dim);border-left-color:var(--primary)}
  .nav-icon{font-size:16px;width:20px;text-align:center}
  .nav-section{font-size:10px;font-weight:600;color:var(--faint);text-transform:uppercase;
                letter-spacing:.08em;padding:16px 20px 6px}
  .main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .topbar{height:58px;border-bottom:1px solid var(--border);display:flex;align-items:center;
           justify-content:space-between;padding:0 28px;background:rgba(255,255,255,0.015);
           flex-shrink:0}
  .topbar-title{font-size:15px;font-weight:600;color:var(--text)}
  .topbar-right{display:flex;align-items:center;gap:12px}
  .status-dot{width:7px;height:7px;border-radius:50%;background:var(--green);
               box-shadow:0 0 6px var(--green);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .content{flex:1;overflow-y:auto;padding:24px 28px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
         padding:20px;margin-bottom:20px}
  .card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px;margin-bottom:20px}
  .table-wrap{overflow-x:auto;border-radius:var(--radius-sm)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:10px 14px;font-size:11px;font-weight:600;color:var(--muted);
      text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border)}
  td{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,0.04)}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,0.02)}
  .footer-note{font-size:11px;color:var(--faint);padding:10px 14px;text-align:center}
  .badge{display:inline-block;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:600}
  .badge-green{background:var(--green-dim);color:var(--green)}
  .badge-red{background:var(--red-dim);color:var(--red)}
  .badge-blue{background:var(--blue-dim);color:var(--blue)}
  .badge-yellow{background:var(--yellow-dim);color:var(--yellow)}
  .badge-purple{background:var(--primary-dim);color:#a5b4fc}
  .btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border:none;
       border-radius:var(--radius-sm);cursor:pointer;font-size:13px;font-weight:500;
       transition:all .15s;text-decoration:none}
  .btn-primary{background:var(--primary);color:#fff}
  .btn-primary:hover{background:#4f46e5}
  .btn-success{background:rgba(74,222,128,0.15);color:var(--green);border:1px solid rgba(74,222,128,0.25)}
  .btn-success:hover{background:rgba(74,222,128,0.25)}
  .btn-sm{padding:5px 10px;font-size:12px}
  .form-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
  .form-group{display:flex;flex-direction:column;gap:5px}
  .form-group label{font-size:12px;color:var(--muted);font-weight:500}
  .form-group input,.form-group select{background:#06101f;border:1px solid var(--border);
    color:var(--text);border-radius:var(--radius-sm);padding:8px 12px;font-size:13px;outline:none;
    transition:border-color .15s}
  .form-group input:focus,.form-group select:focus{border-color:var(--primary)}
  .form-group select option{background:#0f1729}
  .sym{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;color:var(--primary)}
  .page-header{margin-bottom:24px}
  .page-header h2{font-size:20px;font-weight:700;color:var(--text);margin-bottom:4px}
  .page-header p{font-size:13px;color:var(--muted)}
  .error-msg{background:var(--red-dim);color:var(--red);border:1px solid rgba(248,113,113,.2);
              border-radius:var(--radius-sm);padding:10px 14px;margin-bottom:14px;font-size:13px}
  .success-msg{background:var(--green-dim);color:var(--green);border:1px solid rgba(74,222,128,.2);
                border-radius:var(--radius-sm);padding:10px 14px;margin-bottom:14px;font-size:13px}
  .log-entry{padding:5px 0;border-bottom:1px solid rgba(255,255,255,.03);font-size:12px;
              font-family:'JetBrains Mono',monospace;color:#94a3b8}
  .log-entry:last-child{border-bottom:none}
  code{background:#06101f;padding:1px 5px;border-radius:4px;font-size:11px;
       font-family:'JetBrains Mono',monospace;color:#a5b4fc}
  @media(max-width:768px){
    body{flex-direction:column}
    .sidebar{width:100%;min-height:auto;border-right:none;border-bottom:1px solid var(--border)}
    .form-row{grid-template-columns:1fr}
  }
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300..700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
"""

# ── NAV & Layout ──────────────────────────────────────────────────────────────────
NAV = [
    ("/",                   "📊", "Dashboard"),
    ("/agent",              "🤖", "Agent"),
    ("/config",             "⚙️",  "Config"),
    ("/charts/orders",      "📈", "Orders-Chart"),
    (None,                  None, "DEBUG"),
    ("/debug",              "🧩", "Debug-Übersicht"),
    ("/debug/instruments",  "🔍", "Instruments"),
    ("/debug/prices",       "💰", "Prices"),
    ("/debug/log",          "📋", "Log"),
    ("/debug/order",        "📤", "Order-Test"),
    ("/logout",             "🚪", "Logout"),
]


def page(active_path: str, title: str, body: str) -> str:
    nav_html = ""
    for href, icon, label in NAV:
        if icon is None:
            nav_html += f'<div class="nav-section">{label}</div>'
        else:
            cls = "nav-item active" if href == active_path else "nav-item"
            nav_html += (
                f'<a href="{href}" class="{cls}">'
                f'<span class="nav-icon">{icon}</span>{label}</a>'
            )
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{TITLE} · {title}</title>
  {STYLE}
</head>
<body>
  <nav class="sidebar">
    <div class="sidebar-logo">
      <div class="logo-title">hAI.FinOro</div>
      <div class="logo-sub">KI-Trading Agent</div>
    </div>
    {nav_html}
  </nav>
  <div class="main">
    <div class="topbar">
      <span class="topbar-title">{title}</span>
      <div class="topbar-right">
        <span class="status-dot"></span>
        <span style="font-size:12px;color:var(--muted)">Live</span>
      </div>
    </div>
    <div class="content">
      {body}
    </div>
  </div>
</body>
</html>"""


# ── Routen ────────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    cfg      = load_config()
    msg      = ""
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw and pw == cfg.get("ADMIN_PASSWORD", ""):
            session["authenticated"] = True
            _log("Login erfolgreich")
            return redirect(next_url)
        else:
            msg = '<div class="error-msg">❌ Passwort falsch.</div>'
    body = f"""
    <div class="page-header"><h2>Login</h2><p>Passwortschutz für hAI.FinOro</p></div>
    {msg}
    <form method="post">
      <input type="hidden" name="next" value="{next_url}">
      <div class="card">
        <div class="form-group">
          <label>Passwort</label>
          <input type="password" name="password" placeholder="Passwort" autofocus>
        </div>
        <button type="submit" class="btn btn-primary">🔑 Login</button>
      </div>
    </form>
    """
    return page("/login", "Login", body)


@app.route("/logout")
def logout():
    session.clear()
    _log("Logout")
    return redirect(url_for("login"))


@app.route("/")
@require_auth
def index():
    cfg    = load_config()
    mode   = cfg.get("MODE", "observe")
    amount = cfg.get("TRADE_AMOUNT", 0)
    ids, errs = resolve_instrument_ids()
    prices    = get_multi_prices(ids) if ids else {}

    if not cfg.get("API_KEY", "").strip() or not cfg.get("USER_KEY", "").strip():
        cfg_warn = (
            '<div class="error-msg">⚠️ <strong>API-Key oder User-Key fehlt.</strong> '
            'Bitte unter <a href="/config" style="color:var(--red);text-decoration:underline;">'
            'Config</a> eintragen.</div>'
        )
    elif errs:
        cfg_warn = '<div class="error-msg">⚠️ ' + " | ".join(errs[:3]) + "</div>"
    else:
        cfg_warn = ""

    kpis = "".join(
        f'<div class="card" style="padding:16px;">'
        f'<div style="font-size:22px;margin-bottom:4px;">{i["emoji"]}</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-bottom:2px;">{i["symbol"]}</div>'
        f'<div style="font-size:18px;font-weight:700;color:var(--green);">'
        f'{prices.get(i["symbol"], "–")}</div>'
        f"</div>"
        for i in INSTRUMENTS_TO_TRACK
    )
    mode_badge = "green" if mode == "trade" else "blue"
    body = f"""
    {cfg_warn}
    <div class="page-header">
      <h2>Dashboard</h2>
      <p>Echtzeit-Übersicht · Modus: <span class="badge badge-{mode_badge}">{mode.upper()}</span></p>
    </div>
    <div class="card-grid">{kpis}</div>
    <div class="card">
      <div style="font-size:13px;font-weight:600;margin-bottom:12px;">📊 Status</div>
      <table>
        <tr><td style="color:var(--muted)">Modus</td><td><span class="badge badge-{mode_badge}">{mode}</span></td></tr>
        <tr><td style="color:var(--muted)">Trade-Betrag</td><td><strong>{amount}</strong></td></tr>
        <tr><td style="color:var(--muted)">Geladene IDs</td><td><strong>{len(ids)}</strong> / {len(INSTRUMENTS_TO_TRACK)}</td></tr>
        <tr><td style="color:var(--muted)">Letzter Log</td><td style="font-size:12px;color:var(--muted)">{_log_buf[0] if _log_buf else "–"}</td></tr>
      </table>
    </div>"""
    return page("/", "Dashboard", body)


@app.route("/agent", methods=["GET", "POST"])
@require_auth
def agent():
    msg = ""
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "tick":
            try:
                agent_tick()
                msg = '<div class="success-msg">✅ Agent-Tick ausgeführt.</div>'
            except Exception as e:
                msg = f'<div class="error-msg">❌ Fehler: {e}</div>'
        elif action == "signal":
            ids, _  = resolve_instrument_ids()
            prices  = get_multi_prices(ids)
            signal  = get_llm_signal({"prices": prices})
            msg     = f'<div class="success-msg">🤖 LLM-Signal: <strong>{signal}</strong></div>'

    log_html = (
        "".join(f'<div class="log-entry">{e}</div>' for e in list(_log_buf)[:20])
        or '<div style="color:var(--faint);font-size:12px;">Noch keine Einträge.</div>'
    )
    body = f"""
    <div class="page-header"><h2>Agent</h2><p>Manueller Tick oder LLM-Signal abrufen</p></div>
    {msg}
    <div class="card" style="margin-bottom:20px;">
      <form method="post" style="display:flex;gap:12px;flex-wrap:wrap;">
        <button name="action" value="tick"   class="btn btn-primary">▶ Agent-Tick</button>
        <button name="action" value="signal" class="btn" style="background:var(--primary-dim);color:#a5b4fc;border:1px solid var(--border);">🤖 LLM-Signal</button>
      </form>
    </div>
    <div class="card">
      <div style="font-size:13px;font-weight:600;margin-bottom:12px;">📋 Log (letzte {MAX_LOG})</div>
      {log_html}
    </div>"""
    return page("/agent", "Agent", body)


@app.route("/config", methods=["GET", "POST"])
@require_auth
def config():
    msg = ""
    test_result_html = ""
    if request.method == "POST":
        action = request.form.get("_action", "save")
        cfg = load_config()

        if action == "test":
            tr = test_api_keys(
                request.form.get("API_KEY", ""),
                request.form.get("USER_KEY", ""),
                request.form.get("API_URL", ""),
            )
            cls = "success-msg" if tr["ok"] else "error-msg"
            test_result_html = f'<div class="{cls}">' + "<br>".join(tr["messages"]) + "</div>"
        else:
            try:
                raw_interval = request.form.get("INTERVAL", "300").strip()
                try:
                    interval = max(60, min(86400, int(raw_interval)))
                except ValueError:
                    interval = 300
                mode = request.form.get("MODE", "observe").strip().lower()
                if mode not in VALID_MODES:
                    mode = "observe"
                try:
                    amount = float(request.form.get("TRADE_AMOUNT", "0").strip())
                    if amount < 0:
                        amount = 0.0
                except ValueError:
                    amount = 0.0
                admin_pw = request.form.get("ADMIN_PASSWORD", "").strip()
                cfg.update({
                    "API_KEY":         request.form.get("API_KEY", "").strip(),
                    "USER_KEY":        request.form.get("USER_KEY", "").strip(),
                    "API_URL":         request.form.get("API_URL", "").strip().rstrip("/"),
                    "LLM_URL":         request.form.get("LLM_URL", "").strip().rstrip("/"),
                    "LLM_MODEL":       request.form.get("LLM_MODEL", "finance").strip(),
                    "MODE":            mode,
                    "INTERVAL":        interval,
                    "TRADE_AMOUNT":    amount,
                    "MARKET_TIMEZONE": request.form.get("MARKET_TIMEZONE", "Europe/Berlin").strip(),
                    "TRADE_START":     request.form.get("TRADE_START", "08:00").strip(),
                    "TRADE_END":       request.form.get("TRADE_END", "22:00").strip(),
                })
                if admin_pw:
                    cfg["ADMIN_PASSWORD"] = admin_pw
                save_config(cfg)
                _log("Config gespeichert")
                msg = '<div class="success-msg">✅ Konfiguration gespeichert.</div>'
            except Exception as e:
                msg = f'<div class="error-msg">❌ Speicherfehler: {e}</div>'

    cfg = load_config()
    def v(k, d=""):
        return cfg.get(k, d)

    body = f"""
    <div class="page-header"><h2>Konfiguration</h2><p>API-Keys, LLM-URL, Trade-Einstellungen</p></div>
    {msg}{test_result_html}
    <form method="post">
      <input type="hidden" name="_action" value="save">
      <div class="card" style="margin-bottom:16px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:14px;">🔑 eToro API-Zugangsdaten</div>
        <div class="form-row">
          <div class="form-group"><label>API Key (x-api-key)</label><input type="password" name="API_KEY" value="{v('API_KEY')}" placeholder="Dein API-Key"></div>
          <div class="form-group"><label>User Key (x-user-key)</label><input type="password" name="USER_KEY" value="{v('USER_KEY')}" placeholder="Dein User-Key"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>API URL</label><input name="API_URL" value="{v('API_URL','https://public-api.etoro.com/api/v1')}"></div>
          <div class="form-group"></div>
        </div>
      </div>
      <div class="card" style="margin-bottom:16px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:14px;">🤖 LLM-Einstellungen</div>
        <div class="form-row">
          <div class="form-group"><label>LLM URL</label><input name="LLM_URL" value="{v('LLM_URL','https://9router.arbeitermili.eu/v1')}"></div>
          <div class="form-group"><label>LLM Modell</label><input name="LLM_MODEL" value="{v('LLM_MODEL','finance')}"></div>
        </div>
      </div>
      <div class="card" style="margin-bottom:16px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:14px;">⚙️ Agent-Einstellungen</div>
        <div class="form-row">
          <div class="form-group">
            <label>Modus</label>
            <select name="MODE">
              <option value="observe" {"selected" if v("MODE","observe")=="observe" else ""}>observe (nur beobachten)</option>
              <option value="trade"   {"selected" if v("MODE")=="trade" else ""}>trade (echte Orders)</option>
            </select>
          </div>
          <div class="form-group"><label>Interval (Sek, 60–86400)</label><input name="INTERVAL" type="number" min="60" max="86400" value="{v('INTERVAL',300)}"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Trade-Betrag (€)</label><input name="TRADE_AMOUNT" type="number" step="0.01" min="0" value="{v('TRADE_AMOUNT',0)}"></div>
          <div class="form-group"></div>
        </div>
      </div>
      <div class="card" style="margin-bottom:16px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:14px;">🕐 Handelszeit</div>
        <div class="form-row">
          <div class="form-group"><label>Zeitzone</label><input name="MARKET_TIMEZONE" value="{v('MARKET_TIMEZONE','Europe/Berlin')}"></div>
          <div class="form-group"><label>Handelsstart (HH:MM)</label><input name="TRADE_START" value="{v('TRADE_START','08:00')}"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Handelsende (HH:MM)</label><input name="TRADE_END" value="{v('TRADE_END','22:00')}"></div>
          <div class="form-group"></div>
        </div>
      </div>
      <div class="card" style="margin-bottom:20px;">
        <div style="font-size:13px;font-weight:600;margin-bottom:14px;">🔒 Admin-Passwort</div>
        <div class="form-row">
          <div class="form-group"><label>Neues Passwort (leer lassen = unverändert)</label><input type="password" name="ADMIN_PASSWORD" placeholder="Neues Passwort"></div>
          <div class="form-group"></div>
        </div>
      </div>
      <div style="display:flex;gap:10px;">
        <button type="submit" class="btn btn-primary">💾 Speichern</button>
        <button type="submit" name="_action" value="test" class="btn" style="background:var(--primary-dim);color:#a5b4fc;border:1px solid var(--border);">🔌 API-Key testen</button>
      </div>
    </form>"""
    return page("/config", "Konfiguration", body)


@app.route("/charts/orders")
@require_auth
def charts_orders():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT ts, symbol, direction, amount FROM orders ORDER BY id DESC LIMIT 100")
            rows = c.fetchall()
    except sqlite3.Error as e:
        rows = []
        _log(f"SQLite-Fehler charts_orders: {e}")

    if not rows:
        body = """
        <div class="page-header"><h2>Orders-Chart</h2><p>Kein Daten vorhanden.</p></div>
        <div class="card" style="text-align:center;padding:40px;color:var(--muted);">
          📭 Noch keine Orders in der Datenbank.
        </div>"""
        return page("/charts/orders", "Orders-Chart", body)

    labels    = json.dumps([r[0][:16] for r in reversed(rows)])
    amounts   = json.dumps([r[3] for r in reversed(rows)])
    colors    = json.dumps(["rgba(74,222,128,0.7)" if r[2].upper()=="BUY" else "rgba(248,113,113,0.7)" for r in reversed(rows)])

    body = f"""
    <div class="page-header"><h2>Orders-Chart</h2><p>Letzte {len(rows)} Orders aus SQLite</p></div>
    <div class="card">
      <canvas id="ordersChart" style="max-height:400px;"></canvas>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Zeit</th><th>Symbol</th><th>Richtung</th><th>Betrag</th></tr></thead>
          <tbody>
            {"".join(f'<tr><td style=\"font-size:11px;color:var(--muted)\">{r[0][:19]}</td><td><span class=\"sym\">{r[1]}</span></td><td><span class=\"badge {\"badge-green\" if r[2].upper()==\"BUY\" else \"badge-red\"}\">{r[2]}</span></td><td>{r[3]}</td></tr>' for r in rows)}
          </tbody>
        </table>
      </div>
    </div>
    <script>
    new Chart(document.getElementById('ordersChart'), {{
      type: 'bar',
      data: {{
        labels: {labels},
        datasets: [{{
          label: 'Betrag (€)',
          data: {amounts},
          backgroundColor: {colors},
          borderRadius: 4,
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          x: {{ ticks: {{ color: '#64748b', font: {{ size: 10 }} }}, grid: {{ color: 'rgba(99,102,241,0.08)' }} }},
          y: {{ ticks: {{ color: '#64748b' }}, grid: {{ color: 'rgba(99,102,241,0.08)' }} }}
        }}
      }}
    }});
    </script>"""
    return page("/charts/orders", "Orders-Chart", body)


@app.route("/debug")
@require_auth
def debug_overview():
    ids, errors = resolve_instrument_ids()
    prices      = get_multi_prices(ids) if ids else {}
    err_html    = f'<div class="error-msg">⚠ {" · ".join(errors)}</div>' if errors else ""

    rows = "".join(
        f'<tr>'
        f'<td style="font-size:18px;">{i["emoji"]}</td>'
        f'<td><span class="sym">{i["symbol"]}</span></td>'
        f'<td>{i["label"]}</td>'
        f'<td><span class="badge {"badge-green" if ids.get(i["symbol"]) else "badge-red"}">{ids.get(i["symbol"],"–")}</span></td>'
        f'<td style="color:var(--green);font-weight:600;">{prices.get(i["symbol"],"–")}</td>'
        f'</tr>'
        for i in INSTRUMENTS_TO_TRACK
    )
    body = f"""
    <div class="page-header"><h2>Debug-Übersicht</h2><p>Status aller Instrumente & Preise</p></div>
    {err_html}
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th></th><th>Symbol</th><th>Name</th><th>ID</th><th>Preis</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>"""
    return page("/debug", "Debug-Übersicht", body)


@app.route("/debug/instruments")
@require_auth
def debug_instruments():
    ids, errors = resolve_instrument_ids()
    prices      = get_multi_prices(ids) if ids else {}

    auto_rows = "".join(
        f'<tr>'
        f'<td style="font-size:18px;">{i["emoji"]}</td>'
        f'<td><span class="sym">{i["symbol"]}</span></td>'
        f'<td style="color:var(--text);">{i["label"]}</td>'
        f'<td><span class="badge {"badge-green" if ids.get(i["symbol"]) else "badge-red"}">{ids.get(i["symbol"],"–")}</span></td>'
        f'<td style="color:var(--green);font-weight:600;">{prices.get(i["symbol"],"–")}</td>'
        f'</tr>'
        for i in INSTRUMENTS_TO_TRACK
    )
    err_html  = f'<div class="error-msg">⚠ {" · ".join(errors)}</div>' if errors else ""
    hidden    = "".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in ids.items())
    save_form = (
        f'<form method="post" action="/debug/save-ids" style="margin-bottom:20px;">'
        f'{hidden}<button type="submit" class="btn btn-primary">💾 Auto-IDs speichern</button></form>'
        if ids else ""
    )

    query          = request.args.get("q", "").strip()
    search_results = []
    search_error   = ""
    if query:
        search_results, search_error = search_instrument(query)

    result_rows = ""
    if search_results:
        for res in search_results:
            result_rows += f"""
            <tr>
              <td><span class="sym">{res["symbol"]}</span></td>
              <td style="color:var(--text);">{res["name"]}</td>
              <td><span class="badge badge-blue">{res["class"]}</span></td>
              <td><span class="badge badge-green">{res["id"]}</span></td>
              <td>
                <form method="post" action="/debug/save-manual-id" style="display:inline;">
                  <input type="hidden" name="instrument_id" value="{res["id"]}">
                  <input type="hidden" name="symbol"        value="{res["symbol"]}">
                  <input type="hidden" name="q"             value="{query}">
                  <button type="submit" class="btn btn-success btn-sm">💾 Speichern</button>
                </form>
              </td>
            </tr>"""
    elif query and search_error:
        result_rows = f'<tr><td colspan="5" class="error-msg" style="padding:16px;">{search_error}</td></tr>'
    elif query:
        result_rows = '<tr><td colspan="5" style="text-align:center;color:var(--faint);padding:16px;">Keine Ergebnisse.</td></tr>'

    cfg = load_config()
    current_ids_rows = ""
    for inst in INSTRUMENTS_TO_TRACK:
        sym = inst["symbol"]
        iid = cfg.get(f"{sym}_INSTRUMENT_ID")
        if iid:
            current_ids_rows += f"""
            <tr>
              <td style="font-size:16px;">{inst["emoji"]}</td>
              <td><span class="sym">{sym}</span></td>
              <td style="color:var(--text);">{inst["label"]}</td>
              <td><span class="badge badge-green">{iid}</span></td>
              <td>
                <form method="post" action="/debug/delete-id">
                  <input type="hidden" name="symbol" value="{sym}">
                  <button type="submit" class="btn btn-sm" style="background:var(--red-dim);color:var(--red);border:1px solid rgba(248,113,113,.2);">🗑 Entfernen</button>
                </form>
              </td>
            </tr>"""
    if not current_ids_rows:
        current_ids_rows = '<tr><td colspan="5" style="text-align:center;color:var(--faint);padding:16px;">Noch keine IDs gespeichert.</td></tr>'

    search_visible = "block" if query else "none"

    body = f"""
    <div class="page-header">
      <h2>Instrument-IDs</h2>
      <p>Automatische Auflösung + manuelle Suche</p>
    </div>
    <div class="card" style="margin-bottom:20px;">
      <div style="font-size:14px;font-weight:700;color:var(--text);margin-bottom:4px;">🤖 Auto-Resolver</div>
      {err_html}{save_form}
      <div class="table-wrap">
        <table>
          <thead><tr><th></th><th>Symbol</th><th>Name</th><th>ID</th><th>Preis</th></tr></thead>
          <tbody>{auto_rows}</tbody>
        </table>
      </div>
    </div>
    <div class="card" style="margin-bottom:20px;">
      <div style="font-size:14px;font-weight:700;color:var(--text);margin-bottom:4px;">🔎 Manuelle Suche</div>
      <form method="get" action="/debug/instruments" style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap;">
        <input type="text" name="q" value="{query}" placeholder="Symbol oder Name…"
          style="flex:1;min-width:220px;background:#06101f;border:1px solid var(--border);
                 color:var(--text);border-radius:8px;padding:9px 14px;font-size:13px;outline:none;">
        <button type="submit" class="btn btn-primary">🔍 Suchen</button>
        {"<a href='/debug/instruments' class='btn btn-sm' style='background:rgba(100,116,139,.15);color:var(--muted);border:1px solid var(--border);'>✕ Reset</a>" if query else ""}
      </form>
      <div class="table-wrap" style="display:{search_visible};">
        <table>
          <thead><tr><th>Symbol</th><th>Name</th><th>Klasse</th><th>ID</th><th>Aktion</th></tr></thead>
          <tbody>{result_rows}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div style="font-size:14px;font-weight:700;color:var(--text);margin-bottom:4px;">📋 Gespeicherte IDs</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th></th><th>Symbol</th><th>Name</th><th>ID</th><th>Aktion</th></tr></thead>
          <tbody>{current_ids_rows}</tbody>
        </table>
      </div>
    </div>"""
    return page("/debug/instruments", "Instruments", body)


@app.route("/debug/save-ids", methods=["POST"])
@require_auth
def debug_save_ids():
    cfg = load_config()
    for inst in INSTRUMENTS_TO_TRACK:
        sym = inst["symbol"]
        val = request.form.get(sym, "").strip()
        if val.isdigit():
            cfg[f"{sym}_INSTRUMENT_ID"] = int(val)
    save_config(cfg)
    _log("Auto-IDs in config.json gespeichert")
    return Response("", status=302, headers={"Location": "/debug/instruments"})


@app.route("/debug/save-manual-id", methods=["POST"])
@require_auth
def debug_save_manual_id():
    iid    = request.form.get("instrument_id", "").strip()
    symbol = request.form.get("symbol", "").strip().upper()
    q      = request.form.get("q", "")
    if not iid.isdigit():
        return Response("", status=302, headers={"Location": f"/debug/instruments?q={q}"})
    cfg = load_config()
    cfg[f"{symbol}_INSTRUMENT_ID"] = int(iid)
    save_config(cfg)
    _log(f"📌 Manuell gespeichert: {symbol} = {iid}")
    return Response("", status=302, headers={"Location": "/debug/instruments"})


@app.route("/debug/delete-id", methods=["POST"])
@require_auth
def debug_delete_id():
    symbol = request.form.get("symbol", "").strip().upper()
    if not symbol:
        return Response("", status=302, headers={"Location": "/debug/instruments"})
    cfg = load_config()
    key = f"{symbol}_INSTRUMENT_ID"
    if key in cfg:
        del cfg[key]
        save_config(cfg)
        _log(f"🗑 ID entfernt: {symbol}")
    return Response("", status=302, headers={"Location": "/debug/instruments"})


@app.route("/debug/prices")
@require_auth
def debug_prices():
    ids, errors = resolve_instrument_ids()
    prices      = get_multi_prices(ids) if ids else {}
    err_html    = f'<div class="error-msg">⚠ {" · ".join(errors)}</div>' if errors else ""
    rows = "".join(
        f'<tr>'
        f'<td style="font-size:18px;">{i["emoji"]}</td>'
        f'<td><span class="sym">{i["symbol"]}</span></td>'
        f'<td>{i["label"]}</td>'
        f'<td><span class="badge {"badge-green" if ids.get(i["symbol"]) else "badge-red"}">{ids.get(i["symbol"],"–")}</span></td>'
        f'<td style="color:var(--green);font-weight:600;">{prices.get(i["symbol"],"–")}</td>'
        f'</tr>'
        for i in INSTRUMENTS_TO_TRACK
    )
    body = f"""
    <div class="page-header"><h2>Preise</h2><p>Live-Preise aller getrackten Instrumente</p></div>
    {err_html}
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th></th><th>Symbol</th><th>Name</th><th>ID</th><th>Letzter Preis</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <div class="footer-note">Daten von eToro API · Echtzeit</div>
      </div>
    </div>"""
    return page("/debug/prices", "Prices", body)


@app.route("/debug/log")
@require_auth
def debug_log():
    log_entries = list(_log_buf)
    log_html = (
        "".join(f'<div class="log-entry">{e}</div>' for e in log_entries)
        or '<div style="color:var(--faint);font-size:12px;padding:12px;">Noch keine Einträge.</div>'
    )
    body = f"""
    <div class="page-header">
      <h2>System-Log</h2>
      <p>{len(log_entries)} / {MAX_LOG} Einträge</p>
    </div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <div style="font-size:13px;font-weight:600;">📋 Log-Einträge</div>
        <a href="/debug/log" class="btn btn-sm" style="background:var(--primary-dim);color:#a5b4fc;border:1px solid var(--border);">🔄 Refresh</a>
      </div>
      {log_html}
    </div>"""
    return page("/debug/log", "Log", body)


@app.route("/debug/order", methods=["GET", "POST"])
@require_auth
def debug_order():
    result_html = ""
    cfg         = load_config()

    if request.method == "POST":
        mode = cfg.get("MODE", "observe")
        if mode != "trade":
            result_html = '<div class="error-msg">⚠ Modus ist <strong>observe</strong> – Orders deaktiviert. Wechsle in Config auf "trade".</div>'
        else:
            raw_id  = request.form.get("instrument_id", "").strip()
            raw_dir = request.form.get("direction", "buy").strip().lower()
            raw_amt = request.form.get("amount", "1").strip()
            if not raw_id.isdigit():
                result_html = '<div class="error-msg">❌ Ungültige Instrument-ID.</div>'
            elif raw_dir not in ("buy", "sell"):
                result_html = '<div class="error-msg">❌ Ungültige Richtung (buy/sell).</div>'
            else:
                try:
                    amount = float(raw_amt)
                    if amount <= 0:
                        raise ValueError("Betrag muss > 0 sein")
                    result = place_order(int(raw_id), raw_dir, amount)
                    result_html = f'<div class="success-msg">✅ Order-Antwort: <code>{json.dumps(result)}</code></div>'
                    _log(f"Test-Order: ID={raw_id} dir={raw_dir} amt={amount} → {result}")
                except ValueError as e:
                    result_html = f'<div class="error-msg">❌ Fehler: {e}</div>'

    ids, _ = resolve_instrument_ids()
    id_opts = "".join(f'<option value="{v}">{k} (ID: {v})</option>' for k, v in ids.items())

    body = f"""
    <div class="page-header">
      <h2>Order-Test</h2>
      <p>⚠ Nur im Modus <strong>trade</strong> aktiv · Aktuelle config: <span class="badge badge-{"green" if cfg.get("MODE")=="trade" else "blue"}">{cfg.get("MODE","observe")}</span></p>
    </div>
    {result_html}
    <form method="post">
      <div class="card">
        <div style="font-size:13px;font-weight:600;margin-bottom:14px;">📤 Test-Order senden</div>
        <div class="form-row">
          <div class="form-group">
            <label>Instrument-ID</label>
            <select name="instrument_id">
              {id_opts}
              <option value="">Manuell eingeben…</option>
            </select>
          </div>
          <div class="form-group">
            <label>Instrument-ID (manuell)</label>
            <input name="instrument_id" placeholder="z.B. 897840" style="margin-top:4px;">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Richtung</label>
            <select name="direction">
              <option value="buy">BUY</option>
              <option value="sell">SELL</option>
            </select>
          </div>
          <div class="form-group">
            <label>Betrag (€)</label>
            <input name="amount" type="number" step="0.01" min="0.01" value="1.00">
          </div>
        </div>
        <button type="submit" class="btn btn-primary">📤 Order senden</button>
      </div>
    </form>"""
    return page("/debug/order", "Order-Test", body)


@app.route("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}, 200


if __name__ == "__main__":
    init_db()
    _log("hAI.FinOro gestartet")
    app.run(host="0.0.0.0", port=5000, debug=False)
