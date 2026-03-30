from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class TradeZone:
    price: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class AnalysisDecision:
    signal: str
    confidence: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    rationale: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketAnalysisReport:
    symbol: str
    timeframe: str
    generated_at: datetime
    last_price: Decimal
    support_zone: TradeZone
    resistance_zone: TradeZone
    decision: AnalysisDecision
