"""Black-Scholes pricing, greeks and implied volatility (European, as for NSE index options)."""
from __future__ import annotations

import math
from dataclasses import dataclass

_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)
MIN_T = 1e-6


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def intrinsic(spot: float, strike: float, option_type: str) -> float:
    return max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)


def _d1d2(spot: float, strike: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    vs = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / vs
    return d1, d1 - vs


def bs_price(spot: float, strike: float, t: float, r: float, sigma: float, option_type: str) -> float:
    if t <= MIN_T or sigma <= 0:
        return intrinsic(spot, strike, option_type)
    d1, d2 = _d1d2(spot, strike, t, r, sigma)
    df = math.exp(-r * t)
    if option_type == "CE":
        return spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
    return strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point


def bs_greeks(spot: float, strike: float, t: float, r: float, sigma: float, option_type: str) -> Greeks:
    if t <= MIN_T or sigma <= 0:
        itm = intrinsic(spot, strike, option_type) > 0
        delta = (1.0 if itm else 0.0) if option_type == "CE" else (-1.0 if itm else 0.0)
        return Greeks(delta, 0.0, 0.0, 0.0)
    d1, d2 = _d1d2(spot, strike, t, r, sigma)
    sqrt_t = math.sqrt(t)
    pdf = norm_pdf(d1)
    df = math.exp(-r * t)
    gamma = pdf / (spot * sigma * sqrt_t)
    vega = spot * pdf * sqrt_t / 100.0
    decay = -spot * pdf * sigma / (2 * sqrt_t)
    if option_type == "CE":
        return Greeks(norm_cdf(d1), gamma, (decay - r * strike * df * norm_cdf(d2)) / 365.0, vega)
    return Greeks(norm_cdf(d1) - 1.0, gamma, (decay + r * strike * df * norm_cdf(-d2)) / 365.0, vega)


def implied_vol(price: float, spot: float, strike: float, t: float, r: float, option_type: str,
                lo: float = 1e-3, hi: float = 4.0) -> float | None:
    """Bisection IV solver; returns None when the price carries no time value."""
    if t <= MIN_T or price <= 0:
        return None
    floor = bs_price(spot, strike, t, r, lo, option_type)
    if price <= floor or price >= bs_price(spot, strike, t, r, hi, option_type):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, r, mid, option_type) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)
