# app/services/trading.py
"""High‑level trading API used by the Flask routes.

The module glues together the eToro client, safety guards and audit logging.
"""

from typing import Dict, Any

from app.etoro.client import EtoroClient
from app.safety.guards import (
    check_kill_switch,
    check_daily_loss,
    check_drawdown,
    check_position_size,
)
from app.etoro.errors import SafetyViolation


def place_order(
    client: EtoroClient,
    order: Dict[str, Any],
    pnl: float,
    equity: float,
    peak: float,
    units: float,
) -> Dict[str, Any]:
    """Validate an *order* against all safety checks and forward to the API.

    On failure a ``SafetyViolation`` is raised, which the caller can turn into a
    user‑facing error response. Successful orders are returned as the JSON payload
    from the eToro endpoint.

    Args:
        client: Authenticated eToro API client (provides ``config``).
        order: Order payload to send to eToro.
        pnl: Cumulative daily P&L for ``check_daily_loss``.
        equity: Current total portfolio equity for ``check_drawdown``.
        peak: Historical peak equity for ``check_drawdown``.
        units: Absolute size of the new position for ``check_position_size``.
    """
    config = client.config

    # Safety checks – each raises ``SafetyViolation`` on failure.
    check_kill_switch(config)
    check_daily_loss(pnl, config)
    check_drawdown(equity, peak, config)
    check_position_size(units, config)

    # Forward the order to the eToro v2 endpoint (placeholder).
    return client.post("/api/v2/orders", json=order)
