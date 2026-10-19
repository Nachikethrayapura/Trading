"""Option-chain analytics used by institutional desks: OI positioning, dealer gamma, vol surface."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from ..models import OptionChain, OptionQuote, time_to_expiry
from .greeks import bs_greeks, implied_vol
from .indicators import clamp

GEX_EXPIRY_HORIZON_DAYS = 45


def fill_implied_vols(chain: OptionChain, now: datetime, r: float) -> None:
    """Back out IV from the mid price for quotes where the feed did not supply one."""
    for q in chain.quotes:
        if q.iv > 0 or q.mid <= 0:
            continue
        t = time_to_expiry(q.expiry, now)
        iv = implied_vol(q.mid, chain.spot, q.strike, t, r, q.option_type)
        if iv:
            q.iv = iv


def choose_expiry(chain: OptionChain, now: datetime, min_dte: int, max_dte: int) -> date | None:
    for expiry in sorted(chain.expiries):
        dte = (expiry - now.date()).days
        if min_dte <= dte <= max_dte and chain.for_expiry(expiry):
            return expiry
    return None


def pcr(quotes: list[OptionQuote]) -> tuple[float | None, float | None]:
    call_oi = sum(q.oi for q in quotes if q.option_type == "CE")
    put_oi = sum(q.oi for q in quotes if q.option_type == "PE")
    call_vol = sum(q.volume for q in quotes if q.option_type == "CE")
    put_vol = sum(q.volume for q in quotes if q.option_type == "PE")
    return (put_oi / call_oi if call_oi else None, put_vol / call_vol if call_vol else None)


def max_pain(quotes: list[OptionQuote]) -> float | None:
    """Settlement price that minimises the total payout to option holders."""
    strikes = sorted({q.strike for q in quotes})
    if not strikes:
        return None
    calls = [(q.strike, q.oi) for q in quotes if q.option_type == "CE" and q.oi > 0]
    puts = [(q.strike, q.oi) for q in quotes if q.option_type == "PE" and q.oi > 0]

    def payout(settle: float) -> float:
        return (sum(oi * max(settle - k, 0) for k, oi in calls)
                + sum(oi * max(k - settle, 0) for k, oi in puts))

    return min(strikes, key=payout)


def oi_walls(quotes: list[OptionQuote], spot: float) -> tuple[float | None, float | None]:
    """Largest call OI above spot (resistance) and largest put OI below spot (support)."""
    calls = [q for q in quotes if q.option_type == "CE" and q.strike >= spot and q.oi > 0]
    puts = [q for q in quotes if q.option_type == "PE" and q.strike <= spot and q.oi > 0]
    call_wall = max(calls, key=lambda q: q.oi).strike if calls else None
    put_wall = max(puts, key=lambda q: q.oi).strike if puts else None
    return call_wall, put_wall


def _gex_at(quotes: list[tuple[OptionQuote, float]], spot: float, r: float, lot: int) -> float:
    """Dealer gamma exposure in INR per 1% move (calls +, puts -: dealers long calls, short puts)."""
    total = 0.0
    for q, t in quotes:
        g = bs_greeks(spot, q.strike, t, r, q.iv, q.option_type).gamma
        sign = 1.0 if q.option_type == "CE" else -1.0
        total += sign * g * q.oi * lot * spot * spot * 0.01
    return total


def gamma_exposure(chain: OptionChain, now: datetime, r: float) -> tuple[float, float | None]:
    """Net GEX at spot and the gamma-flip level (spot where net GEX crosses zero)."""
    usable = [
        (q, time_to_expiry(q.expiry, now)) for q in chain.quotes
        if q.iv > 0 and q.oi > 0 and 0 <= (q.expiry - now.date()).days <= GEX_EXPIRY_HORIZON_DAYS
    ]
    if not usable:
        return 0.0, None
    spot = chain.spot
    net = _gex_at(usable, spot, r, chain.lot_size)
    grid = [spot * (0.94 + 0.12 * i / 60) for i in range(61)]
    values = [_gex_at(usable, s, r, chain.lot_size) for s in grid]
    flips = []
    for (s0, v0), (s1, v1) in zip(zip(grid, values), zip(grid[1:], values[1:])):
        if v0 == 0 or (v0 < 0) != (v1 < 0):
            flips.append(s0 + (s1 - s0) * (abs(v0) / (abs(v0) + abs(v1) or 1)))
    flip = min(flips, key=lambda s: abs(s - spot)) if flips else None
    return net, flip


def atm_iv(quotes: list[OptionQuote], spot: float) -> float | None:
    strikes = sorted({q.strike for q in quotes if q.iv > 0})
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    ivs = [q.iv for q in quotes if q.strike == atm and q.iv > 0]
    return sum(ivs) / len(ivs) if ivs else None


def skew_25d(quotes: list[OptionQuote], spot: float, t: float, r: float) -> float | None:
    """25-delta put IV minus 25-delta call IV, in vol points (positive = downside fear)."""
    def nearest(option_type: str, target: float) -> OptionQuote | None:
        cands = [q for q in quotes if q.option_type == option_type and q.iv > 0]
        if not cands:
            return None
        return min(cands, key=lambda q: abs(
            bs_greeks(spot, q.strike, t, r, q.iv, option_type).delta - target))

    put, call = nearest("PE", -0.25), nearest("CE", 0.25)
    if not put or not call:
        return None
    return (put.iv - call.iv) * 100


def oi_sentiment(quotes: list[OptionQuote], spot: float, step: float, pcr_oi: float | None,
                 window: int = 5) -> float:
    """Indian-market reading: fresh put writing near ATM is bullish, call writing bearish."""
    near = [q for q in quotes if step and abs(q.strike - spot) <= window * step]
    put_chg = sum(q.oi_change for q in near if q.option_type == "PE")
    call_chg = sum(q.oi_change for q in near if q.option_type == "CE")
    denom = abs(put_chg) + abs(call_chg)
    writing = (put_chg - call_chg) / denom if denom else 0.0
    pcr_component = clamp((pcr_oi - 1.0) / 0.4) if pcr_oi else 0.0
    return clamp(0.6 * writing + 0.4 * pcr_component)


@dataclass
class ChainAnalytics:
    symbol: str
    spot: float
    timestamp: str
    expiry: str
    dte: int
    t_years: float
    strike_step: float
    atm_strike: float
    lot_size: int
    atm_iv: float | None
    expected_move: float | None
    pcr_oi: float | None
    pcr_volume: float | None
    max_pain: float | None
    call_wall: float | None
    put_wall: float | None
    net_gex_cr: float  # INR crore per 1% move
    gamma_flip: float | None
    gex_regime: str  # "positive" (pinning) / "negative" (trending) / "unknown" (no OI data)
    skew_25d: float | None
    oi_sentiment: float
    oi_profile: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_chain(chain: OptionChain, expiry: date, now: datetime, r: float,
                  profile_strikes: int = 15) -> ChainAnalytics:
    fill_implied_vols(chain, now, r)
    quotes = chain.for_expiry(expiry)
    spot = chain.spot
    step = chain.strike_step(expiry)
    t = time_to_expiry(expiry, now)
    strikes = chain.strikes(expiry)
    atm_strike = min(strikes, key=lambda k: abs(k - spot)) if strikes else spot
    iv = atm_iv(quotes, spot)
    if chain.oi_available:
        pcr_oi, pcr_vol = pcr(quotes)
        call_wall, put_wall = oi_walls(quotes, spot)
        net_gex, flip = gamma_exposure(chain, now, r)
        regime = "positive" if net_gex >= 0 else "negative"
        pain, sentiment = max_pain(quotes), oi_sentiment(quotes, spot, step, pcr_oi)
    else:
        pcr_oi = pcr_vol = call_wall = put_wall = flip = pain = None
        net_gex, regime, sentiment = 0.0, "unknown", 0.0

    profile = []
    for k in strikes:
        if step and abs(k - spot) > profile_strikes * step:
            continue
        ce, pe = chain.get(expiry, "CE", k), chain.get(expiry, "PE", k)
        profile.append({
            "strike": k,
            "call_oi": ce.oi if ce else 0, "put_oi": pe.oi if pe else 0,
            "call_oi_chg": ce.oi_change if ce else 0, "put_oi_chg": pe.oi_change if pe else 0,
            "call_iv": round(ce.iv * 100, 2) if ce and ce.iv else None,
            "put_iv": round(pe.iv * 100, 2) if pe and pe.iv else None,
        })

    return ChainAnalytics(
        symbol=chain.symbol, spot=spot, timestamp=chain.timestamp.isoformat(),
        expiry=expiry.isoformat(), dte=(expiry - now.date()).days, t_years=t,
        strike_step=step, atm_strike=atm_strike, lot_size=chain.lot_size,
        atm_iv=iv, expected_move=spot * iv * math.sqrt(t) if iv else None,
        pcr_oi=pcr_oi, pcr_volume=pcr_vol, max_pain=pain,
        call_wall=call_wall, put_wall=put_wall,
        net_gex_cr=net_gex / 1e7, gamma_flip=flip, gex_regime=regime,
        skew_25d=skew_25d(quotes, spot, t, r), oi_sentiment=sentiment,
        oi_profile=profile,
    )
