from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28


def quantize_price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def ema(values: list[Decimal], period: int) -> list[Decimal]:
    if period < 2:
        raise ValueError("period must be >= 2")
    if len(values) < period:
        raise ValueError("values length must be >= period")

    multiplier = Decimal("2") / (Decimal(period) + Decimal("1"))
    seed = sum(values[:period]) / Decimal(period)
    out = [seed]
    prev = seed
    for value in values[period:]:
        current = (value - prev) * multiplier + prev
        out.append(current)
        prev = current
    return out


def rsi(closes: list[Decimal], period: int = 14) -> Decimal:
    if period < 2:
        raise ValueError("period must be >= 2")
    if len(closes) <= period:
        raise ValueError("closes length must be > period")

    gains = Decimal("0")
    losses = Decimal("0")

    for idx in range(1, period + 1):
        delta = closes[idx] - closes[idx - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += abs(delta)

    avg_gain = gains / Decimal(period)
    avg_loss = losses / Decimal(period)

    for idx in range(period + 1, len(closes)):
        delta = closes[idx] - closes[idx - 1]
        gain = max(delta, Decimal("0"))
        loss = max(-delta, Decimal("0"))
        avg_gain = (avg_gain * (Decimal(period) - Decimal("1")) + gain) / Decimal(period)
        avg_loss = (avg_loss * (Decimal(period) - Decimal("1")) + loss) / Decimal(period)

    if avg_loss == 0:
        return Decimal("100")

    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def macd(closes: list[Decimal], fast: int = 12, slow: int = 26, signal_period: int = 9) -> tuple[Decimal, Decimal]:
    if len(closes) < slow + signal_period:
        raise ValueError("insufficient closes for MACD")
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)

    offset = len(fast_ema) - len(slow_ema)
    macd_line = [fast_ema[idx + offset] - slow_ema[idx] for idx in range(len(slow_ema))]
    signal_line = ema(macd_line, signal_period)

    macd_value = macd_line[-1]
    signal_value = signal_line[-1]
    return macd_value, signal_value


def atr(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int = 14) -> Decimal:
    if period < 2:
        raise ValueError("period must be >= 2")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("high, low and close lengths must match")
    if len(closes) <= period:
        raise ValueError("series length must be > period")

    true_ranges: list[Decimal] = []
    for idx in range(1, len(closes)):
        current_high = highs[idx]
        current_low = lows[idx]
        prev_close = closes[idx - 1]
        tr = max(
            current_high - current_low,
            abs(current_high - prev_close),
            abs(current_low - prev_close),
        )
        true_ranges.append(tr)

    running = sum(true_ranges[:period]) / Decimal(period)
    for tr in true_ranges[period:]:
        running = (running * (Decimal(period) - Decimal("1")) + tr) / Decimal(period)
    return running
