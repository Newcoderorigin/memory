from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from .market_indicators import atr, ema, macd, quantize_price, rsi
from .market_models import AnalysisDecision, Candle, MarketAnalysisReport, TradeZone


class MarketAnalysisError(ValueError):
    """Raised when market analysis cannot run safely."""


class MarketAnalysisEngine:
    def analyze(self, *, candles: list[Candle], symbol: str, timeframe: str) -> MarketAnalysisReport:
        self._validate(candles, symbol, timeframe)
        closes = [item.close for item in candles]
        highs = [item.high for item in candles]
        lows = [item.low for item in candles]

        fast_ema = ema(closes, period=9)[-1]
        slow_ema = ema(closes, period=21)[-1]
        rsi_value = rsi(closes, period=14)
        macd_value, macd_signal = macd(closes)
        atr_value = atr(highs, lows, closes, period=14)

        last_price = closes[-1]
        support_price = min(lows[-20:])
        resistance_price = max(highs[-20:])

        trend_up = fast_ema > slow_ema and macd_value >= macd_signal
        trend_down = fast_ema < slow_ema and macd_value <= macd_signal

        rationale: list[str] = [
            f"EMA9={quantize_price(fast_ema)} EMA21={quantize_price(slow_ema)}",
            f"RSI14={quantize_price(rsi_value)}",
            f"MACD={quantize_price(macd_value)} Signal={quantize_price(macd_signal)}",
            f"ATR14={quantize_price(atr_value)}",
        ]

        if trend_up and rsi_value < Decimal("67"):
            signal = "BUY"
            confidence = self._score_confidence(rsi_value, trend_strength=Decimal("0.8"), bullish=True)
            stop_loss = quantize_price(last_price - (atr_value * Decimal("1.5")))
            take_profit = quantize_price(last_price + (atr_value * Decimal("3.0")))
            rationale.append("Momentum and trend alignment indicate bullish continuation.")
        elif trend_down and rsi_value > Decimal("33"):
            signal = "SELL"
            confidence = self._score_confidence(rsi_value, trend_strength=Decimal("0.8"), bullish=False)
            stop_loss = quantize_price(last_price + (atr_value * Decimal("1.5")))
            take_profit = quantize_price(last_price - (atr_value * Decimal("3.0")))
            rationale.append("Momentum and trend alignment indicate bearish continuation.")
        else:
            signal = "HOLD"
            confidence = Decimal("0.52")
            stop_loss = quantize_price(last_price - (atr_value * Decimal("1.0")))
            take_profit = quantize_price(last_price + (atr_value * Decimal("1.0")))
            rationale.append("Signals are mixed; waiting for stronger confirmation is safer.")

        return MarketAnalysisReport(
            symbol=symbol,
            timeframe=timeframe,
            generated_at=datetime.now(UTC),
            last_price=quantize_price(last_price),
            support_zone=TradeZone(
                price=quantize_price(support_price),
                reason="Recent 20-candle low zone",
            ),
            resistance_zone=TradeZone(
                price=quantize_price(resistance_price),
                reason="Recent 20-candle high zone",
            ),
            decision=AnalysisDecision(
                signal=signal,
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                rationale=tuple(rationale),
            ),
        )

    @staticmethod
    def _score_confidence(rsi_value: Decimal, *, trend_strength: Decimal, bullish: bool) -> Decimal:
        if bullish:
            distance = abs(rsi_value - Decimal("55")) / Decimal("45")
        else:
            distance = abs(rsi_value - Decimal("45")) / Decimal("45")
        score = Decimal("0.55") + (trend_strength * Decimal("0.35")) - (distance * Decimal("0.12"))
        return max(Decimal("0.51"), min(Decimal("0.95"), score)).quantize(Decimal("0.01"))

    @staticmethod
    def _validate(candles: list[Candle], symbol: str, timeframe: str) -> None:
        if len(candles) < 40:
            raise MarketAnalysisError("at least 40 candles are required")
        if not symbol.strip():
            raise MarketAnalysisError("symbol is required")
        if not timeframe.strip():
            raise MarketAnalysisError("timeframe is required")
        for index, candle in enumerate(candles):
            if candle.low <= 0 or candle.high <= 0 or candle.close <= 0 or candle.open <= 0:
                raise MarketAnalysisError(f"candle at index {index} contains non-positive prices")
            if candle.low > candle.high:
                raise MarketAnalysisError(f"candle at index {index} has low greater than high")
            if candle.volume < 0:
                raise MarketAnalysisError(f"candle at index {index} has negative volume")
