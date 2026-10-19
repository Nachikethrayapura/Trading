"""Price-based indicators used for the directional (trend) view."""
from __future__ import annotations

import math

from ..models import Bar


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = losses = 0.0
    for prev, cur in zip(values[:period], values[1:period + 1]):
        change = cur - prev
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for prev, cur in zip(values[period:], values[period + 1:]):  # Wilder smoothing
        change = cur - prev
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def true_ranges(bars: list[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: list[Bar], period: int = 14) -> float | None:
    if len(bars) < 2:
        return None
    trs = true_ranges(bars)
    value = sum(trs[:period]) / min(period, len(trs))
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def supertrend(bars: list[Bar], period: int = 10, multiplier: float = 3.0) -> int:
    """Latest Supertrend direction: +1 up, -1 down, 0 if not enough data."""
    if len(bars) <= period:
        return 0
    trs = true_ranges(bars)
    atr_val = sum(trs[:period]) / period
    direction = 1
    final_upper = final_lower = None
    for i in range(period, len(bars)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        b = bars[i]
        mid = (b.high + b.low) / 2
        upper, lower = mid + multiplier * atr_val, mid - multiplier * atr_val
        prev_close = bars[i - 1].close
        if final_upper is None:
            final_upper, final_lower = upper, lower
        else:
            final_upper = upper if upper < final_upper or prev_close > final_upper else final_upper
            final_lower = lower if lower > final_lower or prev_close < final_lower else final_lower
        if b.close > final_upper:
            direction = 1
        elif b.close < final_lower:
            direction = -1
    return direction


def session_vwap(bars: list[Bar]) -> float | None:
    """VWAP of the latest session; falls back to TWAP when the feed has no volume (indices)."""
    if not bars:
        return None
    day = bars[-1].ts.date()
    session = [b for b in bars if b.ts.date() == day]
    typical = [(b.high + b.low + b.close) / 3 for b in session]
    vol = sum(b.volume for b in session)
    if vol > 0:
        return sum(t * b.volume for t, b in zip(typical, session)) / vol
    return sum(typical) / len(typical)


def realized_vol(closes: list[float], periods_per_year: float = 252.0) -> float | None:
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var * periods_per_year)


def trend_score(intraday: list[Bar], daily: list[Bar]) -> tuple[float, dict[str, float]]:
    """Blend of intraday and daily trend signals into a score in [-1, 1]."""
    components: dict[str, float] = {}
    weights: dict[str, float] = {}
    if len(intraday) >= 30:
        closes = [b.close for b in intraday]
        unit = atr(intraday) or 1e-9
        components["ema_cross"] = clamp((ema(closes, 9)[-1] - ema(closes, 21)[-1]) / unit)
        vwap = session_vwap(intraday)
        if vwap:
            components["vwap"] = clamp((closes[-1] - vwap) / (2 * unit))
        components["supertrend"] = float(supertrend(intraday))
        r = rsi(closes)
        if r is not None:
            components["rsi"] = clamp((r - 50) / 20)
        weights.update(ema_cross=0.25, vwap=0.2, supertrend=0.2, rsi=0.15)
    if len(daily) >= 25:
        closes = [b.close for b in daily]
        unit = atr(daily) or 1e-9
        components["daily_ema20"] = clamp((closes[-1] - ema(closes, 20)[-1]) / unit)
        weights["daily_ema20"] = 0.2
    total_w = sum(weights[k] for k in components)
    if total_w == 0:
        return 0.0, components
    score = sum(components[k] * weights[k] for k in components) / total_w
    return clamp(score), components
