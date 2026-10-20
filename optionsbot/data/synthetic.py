"""Option-chain reconstruction from a spot price and an ATM implied vol.

Shared by the market simulator and the historical backtester. Prices are Black-Scholes with a
put-skewed smile and a bid-ask spread that widens (in percent) for cheap far-OTM options.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Callable

import numpy as np

from ..analytics.greeks import bs_price
from ..costs import round_tick
from ..models import OptionChain, OptionQuote, time_to_expiry

# Returns (oi, oi_change, volume) for a strike, or None when the chain carries no OI.
OIModel = Callable[[date, int, float, str, int], tuple[float, float, float]]


def smile_iv(atm_iv: float, spot: float, strike: float, t: float) -> float:
    m = math.log(strike / spot) / (atm_iv * math.sqrt(max(t, 1 / 365)))
    return float(np.clip(atm_iv * (1 - 0.08 * m + 0.025 * m * m), atm_iv * 0.6, atm_iv * 3))


def build_chain(symbol: str, spot: float, atm_iv: float, now: datetime, expiries: list[date], step: float,
                r: float, lot_size: int, oi_model: OIModel | None = None,
                iv_noise: Callable[[], float] | None = None, strikes_each_side: int = 30) -> OptionChain:
    atm = round(spot / step) * step
    quotes = []
    for rank, expiry in enumerate(expiries):
        t = time_to_expiry(expiry, now)
        for i in range(-strikes_each_side, strikes_each_side + 1):
            strike = atm + i * step
            iv = smile_iv(atm_iv, spot, strike, t)
            for option_type in ("CE", "PE"):
                mid = bs_price(spot, strike, t, r, iv, option_type)
                half = max(0.05, mid * 0.005 + 0.05)
                bid = round_tick(mid - half, 0.05) if mid - half > 0.05 else 0.0
                oi, oi_change, volume = oi_model(expiry, rank, strike, option_type, i) if oi_model else (0, 0, 0)
                quotes.append(OptionQuote(
                    strike=strike, option_type=option_type, expiry=expiry,
                    ltp=round_tick(max(mid, 0.05), 0.05), bid=bid, ask=round_tick(mid + half, 0.05),
                    iv=round(iv * (1 + (iv_noise() if iv_noise else 0.0)), 4),
                    oi=oi, oi_change=oi_change, volume=volume,
                ))
    return OptionChain(symbol, round(spot, 2), now, expiries, quotes, lot_size, oi_available=oi_model is not None)
