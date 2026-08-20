# app/services/market.py
"""Market‑related helper functions (price look‑ups, symbol search)."""

from typing import List, Dict, Any

from app.etoro.client import EtoroClient
from app.etoro.endpoints import V1_INSTRUMENTS, V1_INSTRUMENT_ID


def search_instruments(client: EtoroClient, query: str) -> List[Dict[str, Any]]:
    """Return a list of instrument metadata matching *query*.

    The function delegates to ``client.get`` with the proper endpoint and query
    parameters. The raw JSON is returned so callers can pick the fields they need.
    """
    return client.get(V1_INSTRUMENTS, params={"query": query}).get("instruments", [])


def get_instrument(client: EtoroClient, instrument_id: int) -> Dict[str, Any]:
    """Retrieve detailed information for a single instrument by its ID."""
    path = V1_INSTRUMENT_ID.format(id=instrument_id)
    return client.get(path)
