# hAI.FinOro × eToro MCP — Implementation Plan (for GitHub Copilot)

> **Purpose of this document:** This is the single source of truth for implementing an eToro MCP (Model Context Protocol) server inside the existing `hAI.FinOro` repository. It is written to be consumed by an AI coding agent (GitHub Copilot). Follow the phases in order. Do not skip the safety layer. Verify every endpoint against the official docs before implementing (see §5.6).

---

## 1. Project Context

### 1.1 Existing repository

Repository: `jbkunama1/hAI.FinOro` — a Python web application that renders an HTML dashboard.

Current root layout (do not break these files):

```text
hAI.FinOro/
├── .github/
├── .gitignore
├── Dockerfile              # existing, small (431 bytes) — will be extended
├── README.md
├── app.py                  # existing main web app (~60 KB) — entry point of the HTML UI
├── config.example.json     # existing config example
├── docker-compose.yml      # existing, small (467 bytes) — will be extended
├── docs/
├── game.gif
└── requirements.txt        # currently tiny (47 bytes) — will grow
```

**First task for Copilot (Phase 0):** read `app.py`, `Dockerfile`, `docker-compose.yml`, `requirements.txt` and `config.example.json` and determine:
- which web framework is used (Flask or FastAPI or plain),
- how configuration is currently loaded,
- how the app is started in the container.

All new code must integrate with that existing structure. Existing routes and behavior must keep working.

### 1.2 Goal

Extend the repo so that **one single Docker container** provides:

1. The existing HTML dashboard (web UI), extended with eToro pages.
2. A **MCP server over stdio** exposing **at least the 34 tools** listed in §7, runnable via `docker exec -i <container> python -m app.mcp.server`, connectable from Claude Desktop, Claude Code and Cursor.
3. A **shared service layer**: web routes and MCP tools call the same Python services, which call one shared `EtoroClient`. No duplicated API logic.
4. A **safety layer** for all trading operations (demo mode by default, kill switch, execution toggle, limits, allowlist, confirmation, audit log).
5. **Composite analysis tools** on top of the raw API (average entry price, committed cash, pending buy orders, support/resistance).

### 1.3 Reference implementations (read-only inspiration, do not fork)

- `orkblutt/etoro-mcp` (TypeScript) — defines the 34-tool catalog this plan adopts. Known quirk: its client assigns `headers["x-api-key"] = userKey` and `headers["x-user-key"] = apiKey`, which contradicts the official docs. **We follow the official docs** (see §5.2).
- eToro official docs: `https://api-portal.etoro.com/` with machine-readable index at `https://api-portal.etoro.com/llms.txt` and OpenAPI at `https://api-portal.etoro.com/api-reference/openapi.json`.

### 1.4 Ground rules for the coding agent

- Work in a new branch: `feature/etoro-mcp`.
- Python 3.12. Type hints everywhere. `pydantic` for config and domain models.
- Never hardcode credentials. Never log credentials. Redact keys in all log output.
- Every MCP tool returns structured JSON (dict), never formatted human text. Human-readable rendering happens in the web UI or in the agent's chat layer.
- After each phase, run the tests defined for that phase. Do not proceed to the next phase with failing tests.
- Keep the existing web app working: run it and smoke-test existing routes after every phase that touches shared files.

---

## 2. Target Architecture

```text
┌──────────────────────────── Docker container: hai-finoro ───────────────────────────┐
│                                                                                     │
│  Process A (always running, container CMD)                                          │
│  ┌───────────────────────────────────────────────────────────────────────────────┐  │
│  │ Web app (existing app.py, extended)                                           │  │
│  │   HTML pages:  /  /portfolio  /orders  /watchlists  /instrument/<id>  /settings│ │
│  │   JSON API:    /api/health  /api/portfolio  /api/orders  /api/rates ...        │  │
│  │   Port 8080                                                                    │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
│                              │                                                      │
│                              ▼ uses                                                 │
│  ┌───────────────────────────────────────────────────────────────────────────────┐  │
│  │ Service layer (app/services/)                                                 │  │
│  │   MarketService  TradingService  WatchlistService  FeedService  UserService   │  │
│  │   AnalysisService (average entry, exposure, support/resistance)               │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
│                              ▲ uses                                                 │
│  Process B (started on demand via `docker exec -i`)                                 │
│  ┌───────────────────────────────────────────────────────────────────────────────┐  │
│  │ MCP server (app/mcp/server.py, FastMCP, transport=stdio)                      │  │
│  │   34 base tools + ~8 composite tools (§7, §9)                                 │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
│                              │                                                      │
│                              ▼ both use                                             │
│  ┌───────────────────────────────────────────────────────────────────────────────┐  │
│  │ EtoroClient (app/etoro/client.py)                                             │  │
│  │   auth headers · demo/real path routing · rate-limit handling · retries       │  │
│  │ Safety layer (app/safety/) — guards every write/trade call                    │  │
│  │ Audit log (JSONL) → /app/data/audit.log                                       │  │
│  └───────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────┘
         │                                        ▲
         │ HTTP :8080                             │ stdio (docker exec -i)
         ▼                                        │
   Browser (dashboard)                 Claude Desktop / Cursor / Claude Code
```

Key decisions:

| Decision | Choice | Rationale |
|---|---|---|
| Language for MCP server | Python (same as existing app) | One language, one container, shared service layer |
| MCP SDK | official Python SDK: `mcp` package, `FastMCP` | Stdio transport built in, minimal boilerplate |
| MCP transport | stdio via `docker exec -i` | Required by Claude Desktop/Cursor; no exposed network port for MCP |
| HTTP client | `httpx` (sync API is fine) | Timeouts, retries, clean header handling |
| Config | environment variables + optional `config.json` fallback | 12-factor; matches existing `config.example.json` pattern |
| State | filesystem: `/app/data` (JSONL audit log, optional SQLite later) | Simple, volume-mounted |
| Web port | `8080` | Single exposed port |

---

## 3. Configuration

### 3.1 Environment variables (authoritative)

Create/extend `.env.example` with exactly these variables:

```bash
# --- eToro credentials (required) ---
ETORO_API_KEY=          # application API key  -> header x-api-key
ETORO_USER_KEY=         # user-specific key    -> header x-user-key

# --- Trading mode ---
ETORO_TRADING_MODE=demo # demo | real   (default: demo)

# --- Safety layer (defaults are the SAFE values) ---
ETORO_EXECUTION_ENABLED=false          # master switch for ANY write/trade call
ETORO_KILL_SWITCH=true                 # if true: all trading calls fail immediately
ETORO_REQUIRE_CONFIRMATION=true        # trade tools require confirm_token argument
ETORO_CONFIRMATION_PHRASE=CONFIRM      # phrase the confirm_token must match
ETORO_MAX_TRADE_USD=500                # per-order hard limit
ETORO_MAX_DAILY_TRADE_USD=1000         # rolling 24h limit (tracked in audit log)
ETORO_MAX_OPEN_POSITIONS=25            # refuse to open beyond this count
ETORO_SYMBOL_ALLOWLIST=                # comma-separated symbols; empty = all allowed
ETORO_LIVE_TRADING_CONFIRMATION=       # must equal "I_UNDERSTAND" when TRADING_MODE=real

# --- App ---
APP_PORT=8080
LOG_LEVEL=INFO
DATA_DIR=/app/data
```

### 3.2 Config loader

`app/etoro/config.py`:

- `pydantic` `BaseSettings`-style class `EtoroConfig` reading the env vars above.
- Validation rules:
  - `ETORO_API_KEY` and `ETORO_USER_KEY` must be non-empty; fail fast at startup with a clear error message.
  - If `ETORO_TRADING_MODE=real`, then `ETORO_LIVE_TRADING_CONFIRMATION` must equal `I_UNDERSTAND`, otherwise refuse to start the MCP server (web app may still start, but trading tools must be disabled and return a structured error).
  - Parse `ETORO_SYMBOL_ALLOWLIST` into `list[str]` (upper-cased, stripped).
- Expose `is_demo: bool` property.

---

## 4. Project Structure to Create

```text
app/
├── __init__.py
├── etoro/                      # everything that talks to eToro
│   ├── __init__.py
│   ├── config.py               # EtoroConfig (see §3.2)
│   ├── client.py               # EtoroClient (see §5)
│   ├── errors.py               # EtoroApiError, RateLimitError, SafetyViolation
│   ├── models.py               # pydantic models: Instrument, Rate, Candle, Position, Order, Watchlist, FeedPost, UserProfile
│   └── endpoints.py            # path constants + demo/real routing helpers
│
├── services/                   # business logic, shared by web + MCP
│   ├── __init__.py
│   ├── market.py               # MarketService
│   ├── trading.py              # TradingService
│   ├── watchlists.py           # WatchlistService
│   ├── feeds.py                # FeedService
│   ├── users.py                # UserService
│   └── analysis.py             # AnalysisService (composite logic, §9)
│
├── safety/
│   ├── __init__.py
│   ├── guards.py               # check_trading_allowed(), check_order_limits(), require_confirmation()
│   └── audit.py                # append-only JSONL audit log writer/reader
│
├── mcp/
│   ├── __init__.py
│   ├── server.py               # FastMCP bootstrap, entry: python -m app.mcp.server
│   └── tools/
│       ├── __init__.py         # register_all_tools(mcp, services)
│       ├── market_tools.py     # 8 tools
│       ├── trading_tools.py    # 7 tools
│       ├── feed_tools.py       # 4 tools
│       ├── watchlist_tools.py  # 9 tools
│       ├── user_tools.py       # 6 tools
│       └── analysis_tools.py   # composite tools (§9)
│
├── web/                        # NEW web layer additions (integrate with existing app.py)
│   ├── __init__.py
│   ├── routes.py               # new routes registered on the existing app
│   ├── templates/              # base_etoro.html, portfolio.html, orders.html, watchlists.html, instrument.html, settings.html
│   └── static/                 # etoro.css, etoro.js (vanilla, no build step)
│
└── (existing app.py stays the web entry point; it imports and registers app.web.routes)

tests/
├── conftest.py                 # fixtures: mocked EtoroClient (respx), demo config
├── test_client.py
├── test_market_tools.py
├── test_trading_safety.py      # the most important test file — see §12
├── test_analysis.py
└── fixtures/                   # recorded JSON responses (portfolio.json, rates.json, candles.json, ...)

Dockerfile                      # extend existing
docker-compose.yml              # extend existing
.env.example                    # extend/create
docs/ETORO-MCP.md               # user-facing docs (setup, Claude/Cursor config, safety)
```

---

## 5. eToro API — Verified Facts (use these)

### 5.1 Base URL

```text
https://public-api.etoro.com/api/v1
```

### 5.2 Authentication (official)

Every request sends ALL of these headers:

| Header | Value | Required |
|---|---|---|
| `x-api-key` | the **application API key** (`ETORO_API_KEY`) | yes |
| `x-user-key` | the **user key** (`ETORO_USER_KEY`) | yes |
| `x-request-id` | a fresh UUIDv4 per request | yes |
| `Content-Type` | `application/json` | for POST/PUT |

Alternative (not used by us): `Authorization: Bearer <OAuth2 token>`. Never send both auth styles together — the API rejects that.

### 5.3 Demo vs. real routing (verified from the reference implementation)

Two helper methods on the client:

```python
def info_path(self, sub: str) -> str:
    # real: /trading/info/portfolio
    # demo: /trading/info/demo/portfolio
    return f"/trading/info{'/demo' if self.config.is_demo else ''}{sub}"

def execution_path(self, sub: str) -> str:
    # real: /trading/execution/market-open-orders/by-amount
    # demo: /trading/execution/demo/market-open-orders/by-amount
    return f"/trading/execution{'/demo' if self.config.is_demo else ''}{sub}"
```

Market data, feeds, watchlists and user-info paths are **identical** for demo and real (only trading info/execution paths differ).

### 5.4 Rate limits (from official docs)

| Endpoint group | Limit |
|---|---|
| Default (most endpoints) | 60 requests / 60 s, **shared** across all default endpoints |
| Market-data group (`/market-data/*`) | 120 requests / 60 s, shared within the group |
| Creating discussion posts / comments | 20 requests / 60 s, dedicated |

Client requirements:
- Read `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` response headers; log remaining budget at DEBUG level.
- On HTTP 429: read `Retry-After`, wait, retry once. If it fails again, raise `RateLimitError`.
- General retry policy: idempotent GETs retry up to 2 times on 5xx with exponential backoff (0.5 s, 1 s). **Never retry POST/DELETE automatically.**

### 5.5 Verified endpoint paths

Verified against the official docs and/or the working reference implementation. Paths are relative to the base URL.

| Purpose | Method | Path | Status |
|---|---|---|---|
| Search instruments | GET | `/market-data/search?searchText={q}&fields={fields}&pageSize={n}&pageNumber={p}` | verified (note: `fields` query param is **required**, e.g. `fields=displayname,symbol,instrumentId,currentRate,dailyPriceChange,instrumentType`) |
| Instrument display data | GET | `/market-data/instruments?instrumentIds={id1,id2}` | verified |
| Closing prices | GET | `/market-data/instruments/history/closing-price` | verified |
| Candles (OHLCV) | GET | `/market-data/instruments/{instrumentId}/history/candles/{direction}/{period}/{count}` where `direction=asc\|desc`, `period` e.g. `OneMinute,FiveMinutes,FifteenMinutes,ThirtyMinutes,OneHour,FourHours,OneDay,OneWeek`, `count` int | verified pattern — confirm allowed `period` values in docs |
| Instrument types | GET | `/market-data/instrument-types` | confirm exact path in docs |
| Industries | GET | `/market-data/stocks/industries` | confirm exact path in docs |
| Exchanges | GET | `/market-data/exchanges` | confirm exact path in docs |
| Live rates | GET | `/market-data/instruments/rates?instrumentIds={ids}` | confirm exact path in docs (doc page: "Get instrument market rates") |
| Portfolio (positions+orders+credit) | GET | `info_path("/portfolio")` | verified; response schema in §5.7 |
| PnL (real) | GET | `/trading/info/real/pnl` | optional, confirm in docs |
| Open position by amount | POST | `execution_path("/market-open-orders/by-amount")` | verified |
| Open position by units | POST | `execution_path("/market-open-orders/by-units")` | verified |
| Close position | POST | `execution_path("/market-close-orders/positions/{positionId}")` body `{"InstrumentID": <int>, "UnitsToDeduct": <float|null>}` | verified |
| Place limit order | POST | `execution_path("/limit-orders")` | verified |
| Cancel order | DELETE | `execution_path("/orders/{orderId}")` | confirm exact path in docs |
| Instrument feed | GET | `/feeds/instrument/{instrumentId}` | verified |
| User feed | GET | `/feeds/user/{userId}` | verified |
| Create post | POST | `/feeds/posts` (or per docs "Create a new discussion post") | confirm exact path/body in docs |
| Create comment | POST | per docs "Create a comment on a post" | confirm exact path/body in docs |
| Watchlists | GET/POST | `/watchlists` | verified (list/create) |
| Watchlist by id | GET/PUT/DELETE | `/watchlists/{watchlistId}` | verified (delete) — confirm PUT semantics |
| Watchlist items | POST/DELETE | `/watchlists/{watchlistId}/items` (or per docs) | confirm exact path in docs |
| Curated/public lists | GET | per docs "Watchlists" section | confirm exact paths in docs |
| User profile / performance / trades / public portfolio | GET | per docs "Users Info" + "User Stats" sections | confirm exact paths in docs |
| Discover users | GET | per docs "Rankings" section (`/rankings/...`) | confirm exact paths in docs |

### 5.6 Endpoint verification protocol (mandatory)

Before implementing each tool group, Copilot must:

1. Fetch `https://api-portal.etoro.com/llms.txt` and locate the relevant doc page.
2. Fetch the page (append `.md` to the URL for a machine-readable version, e.g. `https://api-portal.etoro.com/api-reference/market-data/search-for-instruments.md`).
3. Extract: exact path, method, query/path params, request body schema, response schema, rate limit.
4. If a path in §5.5 differs from the docs, **the docs win** — update `app/etoro/endpoints.py` and add a code comment with the doc URL and date checked.

### 5.7 Portfolio response schema (verified — use for `models.py`)

`GET /trading/info[/demo]/portfolio` returns:

```json
{
  "clientPortfolio": {
    "credit": 280.35,
    "bonusCredit": 0,
    "positions": [
      {
        "positionID": 2150896073,
        "openDateTime": "2024-08-01T07:44:26.103Z",
        "openRate": 2020.7784,
        "instrumentID": 1002,
        "isBuy": true,
        "takeProfitRate": 0,
        "stopLossRate": 0.0001,
        "mirrorID": 0,
        "amount": 100,
        "leverage": 1,
        "units": 0.049485,
        "initialAmountInDollars": 100,
        "initialUnits": 0.049485,
        "unitsBaseValueDollars": 100,
        "totalFees": 0,
        "isTslEnabled": false,
        "isPartiallyAltered": false,
        "settlementTypeID": 1
      }
    ],
    "orders": [
      {
        "orderID": 5669649,
        "openDateTime": "2024-06-06T08:07:25.083Z",
        "instrumentID": 100043,
        "isBuy": true,
        "rate": 0.1453,
        "amount": 100,
        "leverage": 1,
        "units": 688.231246,
        "takeProfitRate": 0,
        "stopLossRate": 0.00001,
        "executionType": 0
      }
    ],
    "mirrors": [ { "mirrorID": 1841334, "parentUsername": "...", "availableAmount": 560, "positions": [], "ordersForOpen": [] } ]
  }
}
```

Notes for modeling:
- `credit` = available cash in USD. This is the answer to "how much free cash do I have".
- A position's invested capital = `amount` (USD, includes margin top-ups). Entry price = `openRate`. Size = `units`.
- Pending orders live in `clientPortfolio.orders` (and per-mirror in `mirrors[].ordersForOpen`). Aggregate both in `get_orders`.
- `settlementTypeID`: 0=CFD, 1=Real Asset, 2=SWAP, 3=Crypto Margin, 4=Future.

---

## 6. Core Client Skeleton (implement exactly this shape)

`app/etoro/client.py`:

```python
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from .config import EtoroConfig
from .errors import EtoroApiError, RateLimitError

BASE_URL = "https://public-api.etoro.com/api/v1"


class EtoroClient:
    def __init__(self, config: EtoroConfig, timeout: float = 15.0) -> None:
        self.config = config
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout)

    # --- auth & routing -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key,
            "x-user-key": self.config.user_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def info_path(self, sub: str) -> str:
        return f"/trading/info{'/demo' if self.config.is_demo else ''}{sub}"

    def execution_path(self, sub: str) -> str:
        return f"/trading/execution{'/demo' if self.config.is_demo else ''}{sub}"

    # --- HTTP verbs ----------------------------------------------------------
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params, idempotent=True)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=body or {}, idempotent=False)

    def put(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("PUT", path, json=body or {}, idempotent=False)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path, idempotent=False)

    # --- internals -----------------------------------------------------------
    def _request(self, method: str, path: str, *, idempotent: bool, **kwargs: Any) -> Any:
        attempts = 3 if idempotent else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._http.request(method, path, headers=self._headers(), **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    if attempt + 1 < attempts:
                        time.sleep(min(retry_after, 60))
                        continue
                    raise RateLimitError(f"Rate limit exceeded for {method} {path}", retry_after=retry_after)
                if resp.status_code >= 500 and idempotent and attempt + 1 < attempts:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                if resp.status_code >= 400:
                    raise EtoroApiError(resp.status_code, method, path, _safe_body(resp))
                return resp.json() if resp.content else {}
            except httpx.TransportError as exc:  # network-level failure
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise EtoroApiError(None, method, path, str(exc)) from exc
        raise EtoroApiError(None, method, path, f"exhausted retries: {last_exc}")


def _safe_body(resp: httpx.Response) -> str:
    text = resp.text
    return text[:2000]
```

`app/etoro/errors.py`:

```python
class EtoroApiError(Exception):
    def __init__(self, status: int | None, method: str, path: str, detail: str) -> None:
        self.status, self.method, self.path, self.detail = status, method, path, detail
        super().__init__(f"eToro API error {status or 'NETWORK'} on {method} {path}: {detail}")


class RateLimitError(EtoroApiError):
    def __init__(self, message: str, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(429, "GET", "", message)


class SafetyViolation(Exception):
    """Raised when the safety layer blocks an operation."""
```

---

## 7. MCP Tool Catalog — the 34 Base Tools (mandatory, complete)

Implementation pattern per tool (FastMCP):

```python
# app/mcp/tools/market_tools.py
from mcp.server.fastmcp import FastMCP
from app.services.market import MarketService

def register_market_tools(mcp: FastMCP, market: MarketService) -> None:
    @mcp.tool()
    def search_instruments(query: str, page_size: int = 10, page_number: int = 1) -> dict:
        """Search eToro instruments by keyword (e.g. 'AAPL', 'Bitcoin').
        Returns paginated matches with instrumentId, symbol, displayname, type, currentRate."""
        return market.search_instruments(query=query, page_size=page_size, page_number=page_number)
    # ... etc
```

Rules:
- Every tool has a docstring: first line = what it does, then parameter semantics, then what the response contains. Agents read these docstrings — be precise.
- All tools return `dict` (structured JSON). Errors: catch `EtoroApiError`/`SafetyViolation` and return `{"error": {"type": ..., "message": ..., "details": ...}}` instead of raising — this keeps agent conversations recoverable.
- Tool names are `snake_case`, exactly as listed below (agents will reference them by name).

### 7.1 Market Data (8 tools)

| # | Tool | Params | Service call / endpoint |
|---|---|---|---|
| 1 | `search_instruments` | `query: str, page_size: int=10, page_number: int=1` | GET `/market-data/search` |
| 2 | `get_instruments` | `instrument_ids: list[int]` | GET `/market-data/instruments?instrumentIds=...` |
| 3 | `get_instrument_types` | — | GET instrument-types endpoint (§5.5) |
| 4 | `get_industries` | — | GET industries endpoint (§5.5) |
| 5 | `get_exchanges` | — | GET exchanges endpoint (§5.5) |
| 6 | `get_candles` | `instrument_id: int, period: str="OneDay", count: int=100, direction: str="desc"` | GET `/market-data/instruments/{id}/history/candles/{direction}/{period}/{count}` |
| 7 | `get_closing_prices` | — | GET `/market-data/instruments/history/closing-price` |
| 8 | `get_rates` | `instrument_ids: list[int]` | GET rates endpoint (§5.5); return bid/ask/spread per instrument |

### 7.2 Trading (7 tools) — all writes guarded by the safety layer (§8)

| # | Tool | Params | Endpoint | Safety class |
|---|---|---|---|---|
| 9 | `open_position_by_amount` | `instrument_id: int, amount_usd: float, is_buy: bool=true, leverage: int=1, stop_loss_rate: float\|None=None, take_profit_rate: float\|None=None, confirm_token: str=""` | POST `execution_path("/market-open-orders/by-amount")` | TRADE |
| 10 | `open_position_by_units` | `instrument_id: int, units: float, is_buy: bool=true, leverage: int=1, stop_loss_rate: float\|None=None, take_profit_rate: float\|None=None, confirm_token: str=""` | POST `execution_path("/market-open-orders/by-units")` | TRADE |
| 11 | `close_position` | `position_id: int, instrument_id: int, units_to_deduct: float\|None=None, confirm_token: str=""` | POST `execution_path("/market-close-orders/positions/{position_id}")` body `{"InstrumentID": instrument_id, "UnitsToDeduct": units_to_deduct}` | TRADE |
| 12 | `place_limit_order` | `instrument_id: int, rate: float, amount_usd: float, is_buy: bool=true, leverage: int=1, stop_loss_rate: float\|None=None, take_profit_rate: float\|None=None, confirm_token: str=""` | POST `execution_path("/limit-orders")` | TRADE |
| 13 | `cancel_order` | `order_id: int, confirm_token: str=""` | DELETE `execution_path("/orders/{order_id}")` (confirm path per §5.6) | TRADE |
| 14 | `get_orders` | — | GET `info_path("/portfolio")` → aggregate `clientPortfolio.orders` + `mirrors[].ordersForOpen`; enrich each with `symbol`/`displayname` via `get_instruments` | READ |
| 15 | `get_portfolio` | `include_mirrors: bool=true` | GET `info_path("/portfolio")` → normalized positions (add `symbol`, `displayname`, current rate, unrealized PnL per position) + `credit` + `bonusCredit` | READ |

### 7.3 Social Feeds (4 tools)

| # | Tool | Params | Endpoint | Safety class |
|---|---|---|---|---|
| 16 | `get_instrument_feed` | `instrument_id: int, page_size: int=20, page_number: int=1` | GET `/feeds/instrument/{id}` | READ |
| 17 | `get_user_feed` | `user_id: int, page_size: int=20, page_number: int=1` | GET `/feeds/user/{id}` | READ |
| 18 | `create_post` | `text: str, instrument_id: int\|None=None, confirm_token: str=""` | POST per docs (§5.5) | SOCIAL-WRITE |
| 19 | `create_comment` | `post_id: str, text: str, confirm_token: str=""` | POST per docs (§5.5) | SOCIAL-WRITE |

Feed responses can be huge — trim each post to `{id, created, author:{userId,username}, text (max 500 chars), likes, comments}` before returning.

### 7.4 Watchlists (9 tools)

| # | Tool | Params | Endpoint | Safety class |
|---|---|---|---|---|
| 20 | `get_watchlists` | — | GET `/watchlists` | READ |
| 21 | `create_watchlist` | `name: str, instrument_ids: list[int]=[]` | POST `/watchlists` | WRITE |
| 22 | `delete_watchlist` | `watchlist_id: int, confirm_token: str=""` | DELETE `/watchlists/{id}` | WRITE |
| 23 | `rename_watchlist` | `watchlist_id: int, new_name: str` | PUT `/watchlists/{id}` (confirm per §5.6) | WRITE |
| 24 | `add_watchlist_items` | `watchlist_id: int, instrument_ids: list[int]` | POST items endpoint (§5.5) | WRITE |
| 25 | `remove_watchlist_item` | `watchlist_id: int, instrument_id: int` | DELETE item endpoint (§5.5) | WRITE |
| 26 | `set_default_watchlist` | `watchlist_id: int` | per docs | WRITE |
| 27 | `get_curated_lists` | — | per docs | READ |
| 28 | `get_public_watchlists` | `page_size: int=20, page_number: int=1` | per docs | READ |

Watchlist writes do NOT move money, so they require `ETORO_EXECUTION_ENABLED=true` but not the confirmation phrase. (Rationale: practical everyday use.)

### 7.5 User & Discovery (6 tools)

| # | Tool | Params | Endpoint |
|---|---|---|---|
| 29 | `get_user_profile` | `username: str` | per docs "Users Info" |
| 30 | `get_user_performance` | `username: str` | per docs "User Stats" |
| 31 | `get_user_performance_granular` | `username: str, period: str="OneYear"` | per docs "User Stats" |
| 32 | `get_user_trades` | `username: str, page_size: int=20, page_number: int=1` | per docs |
| 33 | `get_user_portfolio` | `username: str` | per docs (public portfolio) |
| 34 | `discover_users` | `query: str="", period: str="CurrYear", min_gain: float\|None=None, max_risk: int\|None=None, page_size: int=10` | per docs "Rankings" |

---

## 8. Safety Layer (non-negotiable)

`app/safety/guards.py` — every TRADE tool must call, in this order:

```python
def guard_trade(config: EtoroConfig, *, symbol: str | None, amount_usd: float | None,
                confirm_token: str, audit: AuditLog) -> None:
    # 1. Kill switch
    if config.kill_switch:
        raise SafetyViolation("Kill switch is ACTIVE. Set ETORO_KILL_SWITCH=false to enable trading.")
    # 2. Execution toggle
    if not config.execution_enabled:
        raise SafetyViolation("Trade execution is DISABLED. Set ETORO_EXECUTION_ENABLED=true to enable.")
    # 3. Confirmation phrase
    if config.require_confirmation and confirm_token != config.confirmation_phrase:
        raise SafetyViolation(
            f"Missing/invalid confirmation. Re-call this tool with confirm_token=\"{config.confirmation_phrase}\" "
            "after the user explicitly confirms the trade."
        )
    # 4. Symbol allowlist
    if symbol and config.symbol_allowlist and symbol.upper() not in config.symbol_allowlist:
        raise SafetyViolation(f"Symbol {symbol} is not in ETORO_SYMBOL_ALLOWLIST.")
    # 5. Per-trade limit
    if amount_usd is not None and amount_usd > config.max_trade_usd:
        raise SafetyViolation(f"Order amount ${amount_usd:.2f} exceeds ETORO_MAX_TRADE_USD=${config.max_trade_usd:.2f}.")
    # 6. Rolling daily limit (from audit log)
    spent_24h = audit.sum_trade_amounts_last_24h()
    if amount_usd is not None and spent_24h + amount_usd > config.max_daily_trade_usd:
        raise SafetyViolation(
            f"Daily limit reached: ${spent_24h:.2f} traded in last 24h, "
            f"limit ${config.max_daily_trade_usd:.2f}."
        )
```

SOCIAL-WRITE tools (`create_post`, `create_comment`) use the same guard but skip checks 5–6 and use their own confirmation requirement.

`app/safety/audit.py`:

- Append-only JSONL at `{DATA_DIR}/audit.log`.
- One line per event: `{"ts": "...", "event": "trade_executed|trade_blocked|order_cancelled|position_closed|watchlist_changed|social_post", "mode": "demo|real", "tool": "...", "params": {...redacted...}, "result_summary": "...", "amount_usd": 123.45}`.
- NEVER write API keys, user keys or confirm tokens into the log.
- Provide `sum_trade_amounts_last_24h()` used by guard check 6.

Additional startup behavior:
- On MCP server start, log (INFO): trading mode, execution enabled?, kill switch state, limits. If mode is `real`, log a prominent WARNING line: `*** REAL TRADING ACTIVE — orders will use real money ***`.

---

## 9. Composite Analysis Tools (the "everyday questions" layer)

`app/services/analysis.py` + `app/mcp/tools/analysis_tools.py`. These tools combine base services so the agent can answer natural questions with ONE call.

### 9.1 `get_account_summary() -> dict`

Combines portfolio + orders:

```json
{
  "mode": "demo",
  "available_cash_usd": 73768.0,
  "bonus_credit_usd": 0.0,
  "open_positions_count": 12,
  "open_positions_value_usd": 28450.0,
  "pending_orders_count": 11,
  "pending_buy_commitment_usd": 11625.0,
  "total_committed_usd": 40075.0,
  "unrealized_pnl_usd": -1234.5
}
```

### 9.2 `get_cash_exposure() -> dict`

- `available_cash_usd` = `clientPortfolio.credit`
- `open_position_exposure_usd` = Σ `positions[].amount`
- `pending_buy_commitment_usd` = Σ `amount` of orders where `isBuy=true`
- `total_committed_usd` = sum of both
- `exposure_by_symbol`: top 10 positions by `amount`, with symbol names resolved

### 9.3 `get_pending_buy_orders(symbol: str|None=None) -> dict`

- All pending orders with `isBuy=true`, grouped by symbol, each with `rate, amount, units, orderID, openDateTime`, plus `total_commitment_usd` and `available_cash_usd` for context.

### 9.4 `get_average_entry_price(symbol: str) -> dict`

Algorithm:
1. `search_instruments(symbol)` → pick best match (exact symbol match preferred) → `instrumentId`.
2. `get_portfolio()` → filter positions by `instrumentID`.
3. Weighted average: `avg = Σ(openRate_i × units_i) / Σ(units_i)`.
4. Also compute `total_units`, `total_invested_usd = Σ amount`, current rate via `get_rates`, `unrealized_pnl_pct = (current/avg − 1) × 100` (respect `isBuy`; for shorts invert).
5. Return all of it as JSON.

### 9.5 `get_projected_average_entry(symbol: str) -> dict`

Same as 9.4, but additionally include pending buy orders for that instrument as if executed at their limit `rate`:
`projected_avg = (Σ openRate×units + Σ orderRate×orderUnits) / (Σ units + Σ orderUnits)`.

### 9.6 `get_support_resistance(instrument_id: int, period: str="OneDay", count: int=120, tolerance_pct: float=1.5) -> dict`

Algorithm (implement in `AnalysisService`):
1. Fetch candles via `get_candles`.
2. Find swing lows/highs: candle `i` is a swing low if `low[i] == min(low[i-k..i+k])` with `k=3` (analogous for swing highs).
3. Cluster swing points: two levels merge if they differ by less than `tolerance_pct` of their mean. Count touches per cluster.
4. `supports` = clusters below current price (sorted by strength = touches × recency weight), `resistances` = clusters above.
5. Classify: `below_support` if price < strongest support × (1 − tolerance), `above_resistance` if price > strongest resistance × (1 + tolerance), else `between`.
6. Return `{current_price, supports: [{level, touches, last_tested}], resistances: [...], status}`.

### 9.7 `get_daily_market_check() -> dict`

- For every distinct instrument in the portfolio: current rate, daily change %, position avg entry, distance to avg entry %.
- Sorted by absolute daily change, worst movers first.

### 9.8 `get_trading_status() -> dict`

Returns the full safety state: mode, execution_enabled, kill_switch, limits, allowlist, trades in last 24h, remaining daily budget. Agents must call this before suggesting trades.

---

## 10. Web Dashboard Integration (HTML)

Integrate into the existing `app.py` app (framework determined in Phase 0). All pages server-side rendered, minimal vanilla JS, reuse the existing app's styling approach. New navigation section "eToro".

| Route | Content | Data source (service layer!) |
|---|---|---|
| `GET /etoro` | Overview cards: available cash, open positions value, pending commitment, unrealized PnL, trading mode + safety state banner | `AnalysisService.get_account_summary`, `get_trading_status` |
| `GET /etoro/portfolio` | Table of open positions: symbol, units, avg entry (openRate), current rate, PnL %, invested $ | `TradingService.get_portfolio` |
| `GET /etoro/orders` | Table of pending orders grouped by symbol, with cancel buttons (POST form → guarded) | `TradingService.get_orders` |
| `GET /etoro/watchlists` | List + detail view of watchlists; add/remove instrument forms | `WatchlistService` |
| `GET /etoro/instrument/<id>` | Instrument detail: rates, candle table/mini-chart (CSS/SVG, no JS libs), support/resistance panel | `MarketService`, `AnalysisService.get_support_resistance` |
| `GET /etoro/settings` | Read-only display of current config (keys redacted: show only first 4 chars + `…`) | `EtoroConfig` |
| `GET /api/health` | `{"status":"ok","mode":"demo","execution_enabled":false,"kill_switch":true}` | — |
| `GET /api/etoro/portfolio` etc. | JSON mirrors of the above for scripting | same services |

Safety in the web UI:
- If `kill_switch` or `!execution_enabled`: show a yellow banner "Trading disabled (demo-safe mode)" on every eToro page.
- Cancel-order forms require a checkbox "I confirm" before the submit button activates.
- Never render credentials.

---

## 11. Docker & Runtime

### 11.1 Dockerfile (extend the existing one — keep its base image choice if reasonable)

Target shape:

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 8080

# Default process: web dashboard. MCP runs on demand via `docker exec`.
CMD ["python", "app.py"]
```

(If the existing Dockerfile uses a different base or entrypoint, adapt but keep: single container, web as CMD, `/app/data` created, port 8080.)

### 11.2 requirements.txt — add

```text
mcp>=1.2.0        # official MCP Python SDK (FastMCP)
httpx>=0.27
pydantic>=2.6
# dev/test (keep in requirements-dev.txt if the repo distinguishes)
pytest>=8.0
respx>=0.21       # httpx mocking
```

Do not remove existing dependencies.

### 11.3 docker-compose.yml (extend existing)

```yaml
services:
  hai-finoro:
    build: .
    container_name: hai-finoro
    restart: unless-stopped
    ports:
      - "8080:8080"
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8080/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
```

### 11.4 MCP server entry point

`app/mcp/server.py`:

```python
from mcp.server.fastmcp import FastMCP

from app.etoro.config import EtoroConfig
from app.etoro.client import EtoroClient
from app.services.market import MarketService
from app.services.trading import TradingService
from app.services.watchlists import WatchlistService
from app.services.feeds import FeedService
from app.services.users import UserService
from app.services.analysis import AnalysisService
from app.safety.audit import AuditLog
from app.mcp.tools import register_all_tools


def build_server() -> FastMCP:
    config = EtoroConfig()                 # validates env, fails fast
    client = EtoroClient(config)
    audit = AuditLog(config.data_dir)
    services = {
        "market": MarketService(client),
        "trading": TradingService(client, config, audit),
        "watchlists": WatchlistService(client, config, audit),
        "feeds": FeedService(client, config, audit),
        "users": UserService(client),
        "analysis": AnalysisService(client),
    }
    mcp = FastMCP("hai-finoro-etoro")
    register_all_tools(mcp, services)
    return mcp


if __name__ == "__main__":
    build_server().run(transport="stdio")
```

### 11.5 Client configurations (put these in `docs/ETORO-MCP.md` verbatim)

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hai-finoro-etoro": {
      "command": "docker",
      "args": ["exec", "-i", "hai-finoro", "python", "-m", "app.mcp.server"]
    }
  }
}
```

Cursor (`.cursor/mcp.json`): same JSON structure.

Claude Code:

```bash
claude mcp add hai-finoro-etoro -- docker exec -i hai-finoro python -m app.mcp.server
```

Local development without Docker (from repo root, with `.env` loaded):

```json
{
  "mcpServers": {
    "hai-finoro-etoro": {
      "command": "python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/absolute/path/to/hAI.FinOro"
    }
  }
}
```

---

## 12. Implementation Phases (execute strictly in order)

### Phase 0 — Recon & branch
- [ ] Create branch `feature/etoro-mcp`.
- [ ] Read existing `app.py`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `config.example.json`. Write a short summary as `docs/EXISTING-APP-NOTES.md` (framework, config loading, startup, existing routes).
- [ ] Fetch `https://api-portal.etoro.com/llms.txt`; save relevant endpoint facts to `docs/ETORO-API-NOTES.md` per §5.6.
- **Done when:** notes files exist; existing app still starts.

### Phase 1 — Config, client, errors, models
- [ ] `app/etoro/config.py`, `errors.py`, `client.py`, `endpoints.py`, `models.py` per §3, §5, §6.
- [ ] `.env.example` per §3.1.
- [ ] Tests: `test_client.py` — header correctness (x-api-key = API key!), demo/real path routing, 429 handling, no-retry-on-POST.
- **Done when:** `pytest tests/test_client.py` green.

### Phase 2 — Market data tools (8)
- [ ] `MarketService` + `market_tools.py` (tools 1–8).
- [ ] Verify each endpoint per §5.6 before implementing.
- [ ] Tests with `respx` fixtures.
- **Done when:** all 8 tools return structured data from mocked API; `get_candles` validates `period` against the allowed list.

### Phase 3 — Trading read tools (14, 15)
- [ ] `TradingService.get_portfolio`, `get_orders` with instrument-name enrichment.
- [ ] Tests using `tests/fixtures/portfolio.json` (build from §5.7 schema).
- **Done when:** positions + orders normalized; credit exposed; mirrors aggregated.

### Phase 4 — Safety layer + trading write tools (9–13)
- [ ] `app/safety/guards.py`, `app/safety/audit.py` per §8.
- [ ] Tools 9–13 with `confirm_token` param.
- [ ] `tests/test_trading_safety.py` — MUST cover: kill switch blocks, execution disabled blocks, wrong confirm_token blocks, amount limit blocks, daily limit blocks, allowlist blocks, happy path in demo mode writes audit line, real mode without `I_UNDERSTAND` refuses startup.
- **Done when:** all safety tests green. No exceptions.

### Phase 5 — Watchlists (20–28)
- [ ] `WatchlistService` + tools. Verify endpoints per §5.6.
- **Done when:** CRUD works against mocked API; writes require `execution_enabled` but not confirm phrase.

### Phase 6 — Feeds (16–19) & User/Discovery (29–34)
- [ ] `FeedService` (with response trimming), `UserService`.
- [ ] Verify endpoints per §5.6 (social-feeds, users-info, user-stats, rankings sections).
- **Done when:** 34 tools registered; `python -m app.mcp.server` starts and lists all tools.

### Phase 7 — Composite analysis tools (§9)
- [ ] `AnalysisService` + `analysis_tools.py` (9.1–9.8).
- [ ] Tests: average entry math (weighted), projected average with pending orders, support/resistance on a synthetic candle fixture, cash exposure sums.
- **Done when:** `tests/test_analysis.py` green.

### Phase 8 — Web dashboard (§10)
- [ ] Routes + templates + nav integration into existing app.
- [ ] Safety banner logic; redacted settings page; health endpoint.
- **Done when:** manual smoke test of all new pages; existing pages unaffected.

### Phase 9 — Docker & client wiring (§11)
- [ ] Update Dockerfile, docker-compose.yml, requirements.
- [ ] `docker compose up -d --build`; healthcheck passes; `docker exec -i hai-finoro python -m app.mcp.server` responds to MCP initialize.
- [ ] Write `docs/ETORO-MCP.md` with setup + client configs + safety explanation.
- **Done when:** Claude Desktop lists all tools via the docker-exec config.

### Phase 10 — Hardening & docs
- [ ] README section: architecture diagram (ASCII), tool list, safety defaults, disclaimer.
- [ ] Add `SECURITY.md` note: never commit `.env`; rotate keys if leaked.
- [ ] Final checklist (§14).

---

## 13. Acceptance Scenarios (test these end-to-end in demo mode)

The implementation is complete when an agent connected via MCP can handle these conversations:

1. **Average entry:** "What's my average entry price on SOL including all positions?" → agent calls `get_average_entry_price("SOL")` and reports units, avg entry, current price, PnL %.
2. **Committed cash:** "How much cash do I have committed right now?" → `get_cash_exposure()` → free cash vs. committed breakdown.
3. **Pending buys:** "List my pending buy orders." → `get_pending_buy_orders()` grouped by symbol with totals.
4. **Support/resistance:** "Is BTC under support or resistance on the daily chart?" → `search_instruments("BTC")` → `get_support_resistance(id, "OneDay", 120)` → status + nearest levels.
5. **Limit order with confirmation:** "Place a buy limit order for 5 SOL at $75." → agent resolves instrument, computes amount, calls `place_limit_order(...)` WITHOUT confirm_token → receives SafetyViolation asking for confirmation → asks user → re-calls with `confirm_token="CONFIRM"` → order placed in DEMO mode, audit line written.
6. **Kill switch:** with `ETORO_KILL_SWITCH=true`, any trade attempt returns a clear structured error and an audit `trade_blocked` event.
7. **Watchlist:** "Create a watchlist 'AI Coins' with SUI, NEAR, TAO." → search → create → add items → confirmation summary.
8. **Dashboard parity:** the numbers shown on `/etoro/portfolio` match what `get_portfolio` returns via MCP (same service layer).

---

## 14. Definition of Done (final checklist)

- [ ] 34 base tools + 8 composite tools registered and listed by the MCP client.
- [ ] Demo mode is default; real mode requires TWO explicit env changes.
- [ ] All safety tests pass; audit log contains blocked and executed trade events.
- [ ] Web dashboard works in the same container on port 8080 and uses the same services.
- [ ] No credentials in code, logs, git history, or rendered HTML.
- [ ] Rate-limit handling verified (429 → wait → single retry → structured error).
- [ ] `docs/ETORO-MCP.md` contains copy-paste-ready Claude Desktop / Cursor / Claude Code configs.
- [ ] README updated with architecture, tool catalog and safety documentation.
- [ ] Branch `feature/etoro-mcp` ready for review/merge.

---

## 15. Troubleshooting Reference (put in docs)

| Symptom | Likely cause | Fix |
|---|---|---|
| 401/403 on every call | keys swapped or empty | verify `x-api-key`=API key, `x-user-key`=user key (NOT swapped) |
| 404 on `/trading/info/portfolio` in demo | missing `/demo` segment | use `info_path()` helper |
| MCP tools not visible in Claude | server not restarted / wrong config | restart Claude Desktop; check `docker ps` container name |
| `docker exec` fails | container not running | `docker compose up -d` first |
| Trades always rejected | safety defaults active | this is intended; adjust env vars consciously |
| 429 errors | shared quota exhausted | back off; check `RateLimit-Remaining`; batch instrument lookups |
| Empty search results | missing `fields` param | `fields` is required on `/market-data/search` |

---

## 16. References

- eToro API docs: https://api-portal.etoro.com/ (index: `/llms.txt`, OpenAPI: `/api-reference/openapi.json`)
- Tool catalog reference: https://github.com/orkblutt/etoro-mcp (34 tools; note the header-swap quirk — do not copy it)
- MCP Python SDK: package `mcp` (FastMCP, stdio transport)
- Base repository: https://github.com/jbkunama1/hAI.FinOro

*Trading involves risk. This software is provided as-is. Always test in demo mode first.*
