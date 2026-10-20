"""Synthetic NSE-like market for demos, tests and after-hours development.

Spot follows a regime-switching jump diffusion whose realised vol runs below implied vol (a
positive volatility risk premium, as in real index options), IV is mean-reverting and
negatively correlated with returns, the smile has a put skew, and OI clusters at round
strikes with intraday put/call writing that reacts to price. Time runs on a simulated clock
that skips nights and weekends, so a full trading day plays out in a few minutes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from ..config import Config
from ..models import IST, MARKET_CLOSE, MARKET_OPEN, Bar, OptionChain, next_weekday, now_ist
from .base import MarketDataProvider
from .synthetic import build_chain

BAR_MINUTES = 5
TRADING_MINUTES_PER_YEAR = 375 * 252
WEEKLY_EXPIRY_SYMBOLS = {"NIFTY"}
EXPIRY_WEEKDAY = 1  # Tuesday
# symbol: (starting spot, strike step, mean ATM IV, OI scale in contracts)
SIM_PARAMS = {
    "NIFTY": (25000.0, 50.0, 0.13, 90_000),
    "BANKNIFTY": (56000.0, 100.0, 0.15, 30_000),
    "FINNIFTY": (26500.0, 50.0, 0.14, 8_000),
    "MIDCPNIFTY": (13000.0, 25.0, 0.17, 12_000),
}
DEFAULT_STOCK_PARAMS = (2500.0, 20.0, 0.25, 3_000)


@dataclass
class _SymbolState:
    spot: float
    step: float
    iv_mean: float
    oi_scale: float
    iv: float
    drift: float = 0.0
    session_open: float = 0.0
    oi_open: dict = field(default_factory=dict)
    intraday: list[Bar] = field(default_factory=list)
    daily: list[Bar] = field(default_factory=list)


def _last_tuesday(year: int, month: int) -> date:
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() != EXPIRY_WEEKDAY:
        d -= timedelta(days=1)
    return d


def sim_expiries(symbol: str, now: datetime, count: int = 4) -> list[date]:
    today = now.date()
    valid = lambda d: d > today or (d == today and now.time() < MARKET_CLOSE)  # noqa: E731
    out: list[date] = []
    if symbol in WEEKLY_EXPIRY_SYMBOLS:
        d = today + timedelta(days=(EXPIRY_WEEKDAY - today.weekday()) % 7)
        while len(out) < count:
            if valid(d):
                out.append(d)
            d += timedelta(days=7)
    y, m = today.year, today.month
    monthly = 0
    while monthly < (1 if symbol in WEEKLY_EXPIRY_SYMBOLS else 3):
        d = _last_tuesday(y, m)
        if valid(d):
            monthly += 1
            if d not in out:
                out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out)


class SimulatedProvider(MarketDataProvider):
    name = "SIMULATED market (synthetic data)"
    simulated = True

    def __init__(self, cfg: Config, warmup_days: int = 260) -> None:
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.data.sim_seed)
        self.vix_closes: list[float] = []
        self.states: dict[str, _SymbolState] = {}
        for symbol in cfg.symbols:
            spot, step, iv, oi = SIM_PARAMS.get(symbol, DEFAULT_STOCK_PARAMS)
            self.states[symbol] = _SymbolState(spot, step, iv, oi, iv)
        # Warm up a year of history, ending at 09:30 on the most recent weekday.
        end = now_ist().date()
        while end.weekday() >= 5:
            end -= timedelta(days=1)
        day = end
        for _ in range(warmup_days):
            day -= timedelta(days=1)
            while day.weekday() >= 5:
                day -= timedelta(days=1)
        self.clock = datetime.combine(day, MARKET_OPEN, tzinfo=IST)
        self._open_session(gap=False)
        target = datetime.combine(end, MARKET_OPEN, tzinfo=IST) + timedelta(minutes=15)
        while self.clock < target:
            self._step_bar()
        for st in self.states.values():
            st.intraday = st.intraday[-5 * 75:]

    # ----------------------------------------------------------------- clock
    def now(self) -> datetime:
        return self.clock

    def advance(self) -> None:
        for _ in range(max(1, self.cfg.data.sim_minutes_per_tick // BAR_MINUTES)):
            self._step_bar()

    def _step_bar(self) -> None:
        if self.clock.time() >= MARKET_CLOSE:
            self._close_session()
            self.clock = datetime.combine(next_weekday(self.clock.date()), MARKET_OPEN, tzinfo=IST)
            self._open_session(gap=True)
        dt = BAR_MINUTES / TRADING_MINUTES_PER_YEAR
        for st in self.states.values():
            self._evolve(st, dt)
        self.clock += timedelta(minutes=BAR_MINUTES)

    def _open_session(self, gap: bool) -> None:
        for st in self.states.values():
            if gap:
                st.spot *= math.exp(self.rng.normal(0, st.iv * 0.25 / math.sqrt(252)))
            st.session_open = st.spot
            st.oi_open = {}

    def _close_session(self) -> None:
        for st in self.states.values():
            bars = [b for b in st.intraday if b.ts.date() == self.clock.date()]
            if bars:
                st.daily.append(Bar(datetime.combine(self.clock.date(), MARKET_OPEN, tzinfo=IST),
                                    bars[0].open, max(b.high for b in bars), min(b.low for b in bars),
                                    bars[-1].close))
            st.intraday = st.intraday[-5 * 75:]
        nifty = self.states.get("NIFTY") or next(iter(self.states.values()))
        self.vix_closes.append(round(nifty.iv * 100 * 1.05, 2))

    def _evolve(self, st: _SymbolState, dt: float) -> None:
        rng = self.rng
        if rng.random() < 0.01:  # trend regime switches roughly every couple of sessions
            st.drift = float(rng.choice([-0.6, 0.0, 0.0, 0.6]))
        realised = st.iv * 0.85  # implied trades above realised on average
        prices = [st.spot]
        shocks = []
        for _ in range(5):
            z = rng.normal()
            shocks.append(z)
            jump = rng.normal(0, 0.012) if rng.random() < 0.0004 else 0.0
            sub = dt / 5
            prices.append(prices[-1] * math.exp((st.drift - 0.5 * realised ** 2) * sub
                                                + realised * math.sqrt(sub) * z + jump))
        z_vol = -0.7 * (sum(shocks) / math.sqrt(5)) + math.sqrt(1 - 0.49) * rng.normal()
        st.iv = float(np.clip(st.iv + 4.0 * (st.iv_mean - st.iv) * dt + 0.9 * st.iv * math.sqrt(dt) * z_vol,
                              st.iv_mean * 0.55, st.iv_mean * 3.0))
        st.spot = prices[-1]
        st.intraday.append(Bar(self.clock, prices[0], max(prices), min(prices), prices[-1]))

    # ----------------------------------------------------------------- data API
    def _base_oi(self, st: _SymbolState, strike: float, option_type: str, expiry_rank: int) -> float:
        anchor = st.session_open
        z = (strike - anchor) / (anchor * st.iv * 0.25)
        bell = math.exp(-0.5 * (z / 1.3) ** 2)
        otm = strike >= anchor if option_type == "CE" else strike <= anchor
        round_bonus = 2.5 if strike % (st.step * 10) == 0 else (1.4 if strike % (st.step * 2) == 0 else 1.0)
        return st.oi_scale * bell * (1.0 if otm else 0.35) * round_bonus / (1 + expiry_rank)

    def option_chain(self, symbol: str) -> OptionChain:
        st = self.states[symbol]
        move = float(np.clip((st.spot / st.session_open - 1) / 0.01, -1, 1))

        def oi_model(expiry: date, rank: int, strike: float, option_type: str, offset: int):
            key = (expiry, option_type, strike)
            if key not in st.oi_open:
                st.oi_open[key] = self._base_oi(st, strike, option_type, rank) * self.rng.uniform(0.8, 1.2)
            near = math.exp(-abs(offset) / 8)
            # Rising market -> put writing, falling market -> call writing.
            flow = 0.25 * move * near * (1 if option_type == "PE" else -1)
            oi = max(st.oi_open[key] * (1 + flow), 0.0)
            return round(oi), round(oi - st.oi_open[key]), round(oi * near * self.rng.uniform(0.5, 2.0))

        return build_chain(symbol, st.spot, st.iv, self.clock, sim_expiries(symbol, self.clock), st.step,
                           self.cfg.risk_free_rate, self.cfg.lot_sizes.get(symbol, 500), oi_model,
                           iv_noise=lambda: self.rng.normal(0, 0.005))

    def intraday_bars(self, symbol: str) -> list[Bar]:
        return list(self.states[symbol].intraday)

    def daily_bars(self, symbol: str) -> list[Bar]:
        st = self.states[symbol]
        today = [b for b in st.intraday if b.ts.date() == self.clock.date()]
        bars = list(st.daily)
        if today:
            bars.append(Bar(today[0].ts, today[0].open, max(b.high for b in today),
                            min(b.low for b in today), today[-1].close))
        return bars

    def india_vix(self) -> float | None:
        nifty = self.states.get("NIFTY") or next(iter(self.states.values()))
        return round(nifty.iv * 100 * 1.05, 2)

    def vix_history(self) -> list[float]:
        return list(self.vix_closes)
