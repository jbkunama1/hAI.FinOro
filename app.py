#!/usr/bin/env python3
from __future__ import annotations
"""
hAI.FinOro — KI-gestützter Trading-Agent
Mit Passwortschutz, SQLite-Tracking und Chart-Ansichten.
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

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konstanten ─────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
TIMEOUT_API = 10
MAX_LOG = 100
VALID_MODES = {"observe", "trade"}
TITLE = "hAI.FinOro"

# ── HTTP-Session mit Retry ─────────────────────────────────────────────────────
http = requests.Session()
http.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
        )
    ),
)

# ── In-Memory-Log ──────────────────────────────────────────────────────────────
_log_buf: deque[str] = deque(maxlen=MAX_LOG)


def _log(msg: str) -> None:
    _log_buf.appendleft(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
    log.info(msg)


# ── Instrumente ────────────────────────────────────────────────────────────────
INSTRUMENTS_TO_TRACK: List[Dict[str, str]] = [
    {"symbol": "BTC", "label": "Bitcoin", "emoji": "₿", "search": "Bitcoin", "cfg_key": "BTC_INSTRUMENT_ID"},
    {"symbol": "ETH", "label": "Ethereum", "emoji": "Ξ", "search": "Ethereum", "cfg_key": "ETH_INSTRUMENT_ID"},
    {"symbol": "GOLD", "label": "Gold (Spot)", "emoji": "🥇", "search": "Gold", "cfg_key": "GOLD_INSTRUMENT_ID"},
    {"symbol": "OIL", "label": "Crude Oil (WTI)", "emoji": "🛢️", "search": "Oil WTI", "cfg_key": "OIL_INSTRUMENT_ID"},
    {"symbol": "EUR", "label": "EUR/USD", "emoji": "€", "search": "EURUSD", "cfg_key": "EURUSD_INSTRUMENT_ID"},
    {"symbol": "GBP", "label": "GBP/USD", "emoji": "£", "search": "GBPUSD", "cfg_key": "GBPUSD_INSTRUMENT_ID"},
]

# ── Default-Config ─────────────────────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    # eToro API
    "API_KEY": "",
    "USER_KEY": "",
    "SECRET_KEY": "change-me",   # für Flask-Session
    "API_URL": "https://public-api.etoro.com/api/v1",
    "BASE_URL": "https://api.etoro.com",
    "SANDBOX": False,
    # LLM
    "LLM_BASE_URL": "https://9router.arbeitermili.eu/v1",
    "LLM_URL": "https://9router.arbeitermili.eu/v1",
    "LLM_MODEL": "finance",
    "LLM_API_KEY": "",
    # Trading allgemein
    "MODE": "observe",
    "INTERVAL": 300,
    "TRADE_AMOUNT": 0.0,
    # Handelszeit
    "MARKET_TIMEZONE": "Europe/Berlin",
    "TRADE_START": "08:00",
    "TRADE_END": "22:00",
    # Instrument IDs (Platzhalter, werden via API/Suche nachgezogen)
    "BTC_INSTRUMENT_ID": 100134,
    "ETH_INSTRUMENT_ID": 100125,
    "GOLD_INSTRUMENT_ID": 559,
    "OIL_INSTRUMENT_ID": 784,
    "EURUSD_INSTRUMENT_ID": 1,
    "GBPUSD_INSTRUMENT_ID": 2,
    # Admin-Passwort & SQLite
    "ADMIN_PASSWORD": "",
    "DB_PATH": "finoro.db",
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


# ── SQLite ─────────────────────────────────────────────────────────────────────
_cfg_for_db = load_config()
DB_PATH = _cfg_for_db.get("DB_PATH", "finoro.db")


def init_db() -> None:
    """Initialisiert SQLite-DB für Orders & Signale."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    instrument_id INTEGER,
                    symbol TEXT,
                    direction TEXT,
                    amount REAL,
                    response_json TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    mode TEXT,
                    prices_json TEXT,
                    signal TEXT
                )
                """
            )
            conn.commit()
        _log(f"SQLite-DB initialisiert: {DB_PATH}")
    except sqlite3.Error as e:
        _log(f"SQLite-Fehler bei init_db: {e}")


def log_order(instrument_id: int, symbol: str, direction: str, amount: float, response: dict) -> None:
    """Speichert eine Order in SQLite."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO orders (ts, instrument_id, symbol, direction, amount, response_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(),
                    instrument_id,
                    symbol,
                    direction.upper(),
                    float(amount),
                    json.dumps(response),
                ),
            )
            conn.commit()
    except sqlite3.Error as e:
        _log(f"SQLite-Fehler beim Loggen der Order: {e}")


def log_signal(mode: str, prices: dict, signal: str) -> None:
    """Speichert ein LLM-Signal in SQLite."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO signals (ts, mode, prices_json, signal)
                VALUES (?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(),
                    mode,
                    json.dumps(prices),
                    signal,
                ),
            )
            conn.commit()
    except sqlite3.Error as e:
        _log(f"SQLite-Fehler beim Loggen des Signals: {e}")


# ── Auth / URLs ────────────────────────────────────────────────────────────────
_cfg_for_app = _cfg_for_db  # bereits geladen
app = Flask(__name__)
app.secret_key = _cfg_for_app.get("SECRET_KEY", "change-me")  # Session-Key


def get_headers() -> Optional[dict]:
    """Auth gemäß eToro API-Portal."""
    cfg = load_config()
    api_key = cfg.get("API_KEY", "").strip()
    user_key = cfg.get("USER_KEY", "").strip()

    if not api_key or not user_key:
        _log("API_KEY oder USER_KEY fehlen in config.json")
        return None

    return {
        "x-api-key": api_key,
        "x-user-key": user_key,
        "Accept": "application/json",
    }


def get_llm_headers() -> dict:
    cfg = load_config()
    lk = cfg.get("LLM_API_KEY", "").strip()
    h: dict = {"Content-Type": "application/json"}
    if lk:
        h["Authorization"] = f"Bearer {lk}"
    return h


def api_url(path: str = "") -> str:
    cfg = load_config()
    base = cfg.get("API_URL", "https://public-api.etoro.com/api/v1").rstrip("/")
    return base + path


def llm_url(path: str = "") -> str:
    cfg = load_config()
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
            _log(f"401 Unauthorized für {path} – prüfe API_KEY / USER_KEY / Verifizierung im API-Portal.")
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


# ── Passwortschutz (Session) ───────────────────────────────────────────────────
def is_authenticated() -> bool:
    return session.get("authenticated") is True


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if is_authenticated():
            return fn(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_config()
    msg = ""
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
          <input type="password" name="password" placeholder="Passwort">
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


# ── API-Key-Test ───────────────────────────────────────────────────────────────
def test_api_keys(api_key: str, user_key: str, api_url_cfg: str) -> dict:
    """Testet API_KEY + USER_KEY gegen Market-Data-Rates-Endpoint."""
    result = {"ok": False, "messages": []}

    api_key = api_key.strip()
    user_key = user_key.strip()
    api_url_cfg = api_url_cfg.strip().rstrip("/") or "https://public-api.etoro.com/api/v1"

    if not api_key or not user_key:
        result["messages"].append(
            "API_KEY oder USER_KEY fehlen. "
            "Im eToro API-Portal unter Settings → Trading → API Key Management "
            "einen Key anlegen und Werte im Config-Formular eintragen."
        )
        return result

    test_instrument_id = DEFAULT_CONFIG.get("BTC_INSTRUMENT_ID", 100134)

    try:
        r = http.get(
            f"{api_url_cfg}/market-data/instruments/rates",
            headers={
                "x-api-key": api_key,
                "x-user-key": user_key,
                "x-request-id": str(uuid.uuid4()),
                "Accept": "application/json",
            },
            params={"instrumentIds": test_instrument_id},
            timeout=TIMEOUT_API,
        )
    except requests.ConnectionError as e:
        result["messages"].append(f"Verbindungsfehler zur eToro API: {e}")
        result["messages"].append(
            "Prüfe Internetverbindung, Firewall/Proxy und ob die URL "
            f"{api_url_cfg} erreichbar ist."
        )
        return result
    except requests.Timeout:
        result["messages"].append("Timeout bei der eToro API.")
        result["messages"].append(
            "Eventuell kurzzeitig überlastet – später erneut testen oder TIMEOUT_API erhöhen."
        )
        return result
    except Exception as e:
        result["messages"].append(f"Unerwarteter Fehler: {e}")
        return result

    if r.status_code == 200:
        result["ok"] = True
        result["messages"].append(
            "✅ API-Key-Test erfolgreich: Market-Data-Endpunkt liefert Daten."
        )
        return result

    if r.status_code == 401:
        result["messages"].append(
            "❌ 401 Unauthorized – die Kombination aus API_KEY und USER_KEY ist "
            "für diese API nicht gültig."
        )
        return result

    if r.status_code == 403:
        result["messages"].append("❌ 403 Forbidden – Zugang zur API ist blockiert.")
        return result

    if r.status_code == 404:
        result["messages"].append(
            f"❌ 404 Not Found für {api_url_cfg}/market-data/instruments/rates."
        )
        return result

    result["messages"].append(f"❌ HTTP {r.status_code}: {r.text[:200]}")
    return result


# ── Handelszeit ────────────────────────────────────────────────────────────────
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
    end = parse_hhmm(cfg.get("TRADE_END", "23:59"))

    if not start or not end:
        _log("TRADE_START/TRADE_END in config.json ungültig – kein Handel.")
        return False

    if start <= end:
        return start <= now_local <= end
    else:
        return now_local >= start or now_local <= end


# ── Preis-Abfrage (nur rates-Endpoint) ────────────────────────────────────────
def get_price(instrument_id: int) -> Optional[str]:
    """Holt Preis über Market-Data-Rates."""
    r = api_get("/market-data/instruments/rates", params={"instrumentIds": instrument_id})
    if not r:
        _log(f"Rates-Request für Instrument-ID {instrument_id}: kein Response")
        return None

    if r.status_code != 200:
        _log(
            f"Rates-Request für Instrument-ID {instrument_id} -> HTTP {r.status_code}: "
            f"{r.text[:200]}"
        )
        return None

    try:
        data = r.json()
    except ValueError as e:
        _log(f"JSON-Fehler beim Parsen der Rates-Antwort für {instrument_id}: {e}")
        return None

    rates = data.get("rates") or data.get("items") or []
    if not rates:
        _log(f"Keine rates-Einträge für Instrument-ID {instrument_id} in Response: {data}")
        return None

    entry = rates[0]
    val = entry.get("lastExecution") or entry.get("bid") or entry.get("ask")
    if val is None:
        _log(f"Kein Preisfeld in rates-Entry für Instrument-ID {instrument_id}: {entry}")
        return None

    return str(val)



def get_multi_prices(ids: dict) -> dict:
    prices = {}
    for sym, iid in ids.items():
        p = get_price(int(iid))
        if p is not None:
            prices[sym] = p
    return prices


# ── Instrument-Suche & IDs ─────────────────────────────────────────────────────
def search_instrument(query: str) -> Tuple[List[dict], Optional[str]]:
    """Sucht Instrument (Symbol oder Name)."""
    headers = get_headers()
    if headers is None:
        return [], "API-Keys fehlen in config.json."

    results: List[dict] = []
    errors: List[str] = []

    endpoints = [
        (api_url("/market-data/instruments"), {"symbol": query.upper(), "limit": 10}),
        (api_url("/market-data/instruments"), {"query": query, "limit": 10}),
        (api_url("/instruments"), {"q": query, "limit": 10}),
        (api_url("/instruments/search"), {"query": query}),
        (api_url(f"/instruments/{query.upper()}"), {}),
    ]

    for url, params in endpoints:
        try:
            kw: dict = dict(headers={**headers, "x-request-id": str(uuid.uuid4())}, timeout=TIMEOUT_API)
            if params:
                kw["params"] = params
            r = http.get(url, **kw)
            if r.status_code == 200:
                data = r.json()
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
                    iid = (
                        item.get("instrumentId")
                        or item.get("InstrumentId")
                        or item.get("id")
                    )
                    sym = (
                        item.get("internalSymbol")
                        or item.get("symbol")
                        or item.get("ticker")
                        or "?"
                    )
                    name = (
                        item.get("displayName")
                        or item.get("displayname")
                        or item.get("name")
                        or "?"
                    )
                    cls = (
                        item.get("assetClass")
                        or item.get("instrumentType")
                        or item.get("type")
                        or "?"
                    )
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
    cfg = load_config()
    ids: Dict[str, int] = {}
    errors: List[str] = []

    for inst in INSTRUMENTS_TO_TRACK:
        sym = inst["symbol"]
        cfg_key = inst.get("cfg_key", f"{sym}_INSTRUMENT_ID")

        cached = cfg.get(cfg_key)
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


# ── LLM-Signal & Order ────────────────────────────────────────────────────────
def get_llm_signal(context: dict) -> str:
    cfg = load_config()
    model = cfg.get("LLM_MODEL", "finance")
    prompt = (
        f"Du bist ein Trading-Assistent. Analysiere:\n{json.dumps(context, indent=2)}\n"
        "Antworte mit BUY, SELL oder HOLD + kurze Begründung (max 2 Sätze)."
    )

    endpoints_to_try = [
        (
            llm_url("/chat/completions"),
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.2,
            },
        ),
        (
            llm_url("/completions"),
            {
                "model": model,
                "prompt": prompt,
                "max_tokens": 150,
                "temperature": 0.2,
            },
        ),
        (
            cfg.get("LLM_BASE_URL", "http://localhost:11434").rstrip("/") + "/api/generate",
            {"model": model, "prompt": prompt, "stream": False},
        ),
    ]

    for ep, payload in endpoints_to_try:
        try:
            r = http.post(ep, headers=get_llm_headers(), json=payload, timeout=30)
            if r.status_code == 200:
                d = r.json()
                if "choices" in d:
                    msg = d["choices"][0]
                    return (
                        msg.get("message", {}).get("content")
                        or msg.get("text")
                        or "HOLD"
                    ).strip()
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
        "direction": direction.upper(),
        "amount": amount,
        "type": "market",
    }
    try:
        r = http.post(
            api_url("/orders"),
            headers={**headers, "x-request-id": str(uuid.uuid4())},
            json=payload,
            timeout=TIMEOUT_API,
        )
        result = r.json() if r.status_code in (200, 201) else {"error": r.text}
        # Order in SQLite loggen
        log_order(instrument_id, symbol or "?", direction, amount, result)
        return result
    except Exception as e:
        err = {"error": str(e)}
        log_order(instrument_id, symbol or "?", direction, amount, err)
        return err



def agent_tick() -> None:
    cfg = load_config()
    mode = cfg.get("MODE", "observe")
    if mode not in VALID_MODES:
        _log(f"Ungültiger Modus {mode!r}, setze auf observe")
        mode = "observe"

    ids, errs = resolve_instrument_ids()
    if errs:
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
            _log("Außerhalb der konfigurierten Handelszeit – keine Orders.")
            return
        if signal.startswith("BUY"):
            iid = ids.get("BTC")
            amount = float(cfg.get("TRADE_AMOUNT", 0))
            if iid and amount > 0:
                result = place_order(iid, "buy", amount, "BTC")
                _log(f"Order-Ergebnis: {result}")


# ── Flask-App / UI ─────────────────────────────────────────────────────────────
STYLE = """
<style>
/* Dein bisheriges CSS hier einsetzen oder belassen */
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300..700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
"""

NAV = [
    ("/", "📊", "Dashboard"),
    ("/agent", "🤖", "Agent"),
    ("/config", "⚙️", "Config"),
    ("/charts/orders", "📈", "Orders-Chart"),
    (None, None, "DEBUG"),
    ("/debug", "🧩", "Debug-Übersicht"),
    ("/debug/instruments", "🔍", "Instruments"),
    ("/debug/prices", "💰", "Prices"),
    ("/debug/log", "📋", "Log"),
    ("/debug/order", "📤", "Order-Test"),
    ("/logout", "🚪", "Logout"),
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


@app.route("/")
@require_auth
def index():
    cfg = load_config()
    mode = cfg.get("MODE", "observe")
    amount = cfg.get("TRADE_AMOUNT", 0)
    ids, errs = resolve_instrument_ids()
    prices = get_multi_prices(ids) if ids else {}

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


# (Agent-, Config-, Charts- und Debug-Routen hier wie zuvor, alle mit @require_auth)


@app.route("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}, 200


if __name__ == "__main__":
    init_db()
    _log("hAI.FinOro gestartet")
    app.run(host="0.0.0.0", port=5000, debug=False)
