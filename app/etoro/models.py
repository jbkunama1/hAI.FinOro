# app/etoro/models.py
"""Pydantic models for eToro entities used by the client."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, validator

class Instrument(BaseModel):
    id: int = Field(..., description="Instrument identifier")
    symbol: str = Field(..., description="Ticker symbol, e.g., 'BTCUSD'")
    name: Optional[str] = None
    asset_class: Optional[str] = None
    exchange: Optional[str] = None

    @validator("symbol")
    def normalize_symbol(cls, v: str) -> str:
        return v.upper()

class Position(BaseModel):
    instrument: Instrument
    units: float = Field(..., description="Number of units held")
    avg_price: float = Field(..., description="Average acquisition price")
    current_price: Optional[float] = None
    pnl: Optional[float] = None
    last_update: Optional[datetime] = None

class PortfolioInstrument(BaseModel):
    instrument: Instrument
    positions: List[Position]
    net_pnl: float = 0.0
    equity: float = 0.0

class Portfolio(BaseModel):
    account_currency: str
    total_equity: float
    cash_available: float
    total_pnl: float
    instruments: List[PortfolioInstrument]

class OrderResponse(BaseModel):
    order_id: int
    status: str
    created_at: datetime
    instrument_id: int
    units: float
    price: Optional[float] = None
    side: str
    type: str
    fill_price: Optional[float] = None
    fill_timestamp: Optional[datetime] = None
