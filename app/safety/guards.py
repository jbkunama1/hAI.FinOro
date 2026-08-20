# app/safety/guards.py
"""Safety guards for trading actions.

The guard functions raise ``SafetyViolation`` (from ``app.etoro.errors``) when the
configured limits are exceeded. The ``audit`` function writes a JSON line to the
audit log – this file is mounted as a Docker volume at ``/app/data`` so the
operator can review every decision.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from app.etoro.errors import SafetyViolation
from app.etoro.config import EtoroConfig

log = logging.getLogger(__name__)

# Resolve the audit log location – default to a ``data`` directory next to the
# repo root. ``APP_ROOT`` is set by the Docker image entrypoint.
_AUDIT_LOG = Path(os.getenv("APP_ROOT", "."), "data", "audit.log")
_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)


def _write_audit(entry: Dict[str, Any]) -> None:
    """Append a JSONL entry to the audit log.

    Each entry contains a timestamp and a ``type`` field that indicates the
    guard that performed the check. ``entry`` can hold arbitrary payload data.
    """
    entry.setdefault("timestamp", datetime.utcnow().isoformat() + "Z")
    with _AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_kill_switch(config: EtoroConfig) -> None:
    """Abort any trading operation if the global ``kill_switch`` flag is ``True``.

    The flag is stored in the config file and can be toggled at runtime via the
    dashboard. ``SafetyViolation`` is raised so callers can catch and log the
    failure uniformly.
    """
    if getattr(config, "kill_switch", False):
        _write_audit({"type": "kill_switch", "action": "blocked"})
        raise SafetyViolation("kill_switch", "Trading disabled by kill‑switch")


def check_daily_loss(pnl: float, config: EtoroConfig) -> None:
    """Enforce the maximum daily loss limit.

    ``pnl`` is the cumulative profit‑and‑loss for the current UTC day.  If it
    drops below ``-config.max_daily_loss`` the guard blocks the trade.
    """
    limit = getattr(config, "max_daily_loss", None)
    if limit is not None and pnl < -limit:
        _write_audit({"type": "daily_loss", "pnl": pnl, "limit": limit, "action": "blocked"})
        raise SafetyViolation("daily_loss", f"Cumulative loss {pnl:.2f} exceeds limit {limit:.2f}")


def check_drawdown(equity: float, peak: float, config: EtoroConfig) -> None:
    """Enforce a drawdown percentage based on the historical peak equity.

    ``equity`` – current total equity.
    ``peak``   – highest equity observed earlier today (or since the bot started).
    ``config.max_drawdown_pct`` is expressed as a percent value.
    """
    max_pct = getattr(config, "max_drawdown_pct", None)
    if max_pct is not None:
        # ``peak`` may equal ``equity`` at start; avoid division‑by‑zero.
        if peak > 0:
            drawdown_pct = (peak - equity) / peak * 100
            if drawdown_pct > max_pct:
                _write_audit(
                    {
                        "type": "drawdown",
                        "equity": equity,
                        "peak": peak,
                        "drawdown_pct": drawdown_pct,
                        "limit": max_pct,
                        "action": "blocked",
                    }
                )
                raise SafetyViolation(
                    "drawdown",
                    f"Drawdown {drawdown_pct:.2f}% exceeds limit {max_pct}%",
                )


def check_position_size(units: float, config: EtoroConfig) -> None:
    """Validate that a new position does not exceed ``MAX_POSITION_SIZE``.

    ``units`` is the absolute quantity the bot wants to open (positive for a buy,
    negative for a sell). ``MAX_POSITION_SIZE`` is defined in the same currency
    units as ``units`` (e.g., USD for stocks, BTC for crypto).
    """
    limit = getattr(config, "max_position_size", None)
    if limit is not None and abs(units) > limit:
        _write_audit({"type": "position_size", "units": units, "limit": limit, "action": "blocked"})
        raise SafetyViolation(
            "position_size",
            f"Requested size {abs(units)} exceeds configured limit {limit}",
        )


def audit_action(action: str, payload: Dict[str, Any] | None = None) -> None:
    """Write a generic audit entry for any successful operation.

    ``action`` is a short string like ``"order_placed"``; ``payload`` may contain
    additional context such as the order ID or instrument.
    """
    entry = {"type": "action", "action": action}
    if payload:
        entry.update(payload)
    _write_audit(entry)
