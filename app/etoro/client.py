# app/etoro/client.py
from __future__ import annotations
import httpx
import json
import uuid
import time
import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

from app.etoro.config import EtoroConfig
from app.etoro.errors import EtoroApiError, RateLimitError

log = logging.getLogger(__name__)

class EtoroClient:
    """HTTP client for the eToro API."""

    def __init__(self, config: EtoroConfig):
        self.config = config
        self._client = httpx.Client(base_url=config.api_url, timeout=10.0)
        self._base_client = httpx.Client(base_url=config.base_url, timeout=10.0)

    def _headers(self, require_user_key: bool = False) -> Dict[str, str]:
        headers = {
            "X-API-KEY": self.config.api_key,
            "X-Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        if require_user_key:
            if not self.config.user_key:
                raise EtoroApiError("User key required but not configured")
            headers["X-USER-KEY"] = self.config.user_key
        return headers

    def _request(self, method: str, path: str, client_type: str = "api", **kwargs) -> Any:
        client = self._client if client_type == "api" else self._base_client
        url = path  # Use relative path; httpx client base_url resolves full URL

        try:
            log.debug(f"Requesting {method} {url} with client {client_type}")
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            log.error(f"eToro API HTTP error for {url}: {e.response.status_code} - {e.response.text}")
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 0))
                raise RateLimitError(status_code=e.response.status_code, response_data=e.response.json(), retry_after=retry_after)
            raise EtoroApiError(f"HTTP error: {e.response.status_code} {e.response.text}", status_code=e.response.status_code, response_data=e.response.json()) from e
        except httpx.RequestError as e:
            log.error(f"eToro API request error for {url}: {e}")
            raise EtoroApiError(f"Request error: {e}") from e

    def get(self, path: str, client_type: str = "api", require_user_key: bool = False, **kwargs) -> Any:
        return self._request("GET", path, client_type=client_type, headers=self._headers(require_user_key), **kwargs)

    def post(self, path: str, client_type: str = "api", require_user_key: bool = False, **kwargs) -> Any:
        return self._request("POST", path, client_type=client_type, headers=self._headers(require_user_key), **kwargs)

    # Example: get instrument by ID
    def get_instrument_by_id(self, instrument_id: int) -> Optional[Dict[str, Any]]:
        path = f"instruments/{instrument_id}"
        return self.get(path)

    # Example: search instruments
    def search_instruments(self, query: str) -> List[Dict[str, Any]]:
        path = "instruments"
        params = {"query": query}
        return self.get(path, params=params).get("instruments", [])
