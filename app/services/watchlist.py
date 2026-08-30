# app/services/watchlist.py
"""Utility functions for handling watchlists.

At the moment only a thin wrapper around the eToro API is provided; the module will be
expanded as the feature set grows.
"""

from typing import List, Dict, Any

from app.etoro.client import EtoroClient
from app.etoro.endpoints import V1_WATCHLISTS


def list_watchlists(client: EtoroClient) -> List[Dict[str, Any]]:
    """Return a list of watchlists belonging to the authenticated user."""
    return client.get(V1_WATCHLISTS).get("watchlists", [])


def add_to_watchlist(client: EtoroClient, watchlist_id: int, instrument_id: int) -> Dict[str, Any]:
    """Add *instrument_id* to *watchlist_id*.

    The function performs a ``POST`` request to the appropriate endpoint.
    """
    path = f"{V1_WATCHLISTS}/{watchlist_id}/items"
    return client.post(path, json={"instrument_id": instrument_id})
