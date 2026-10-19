"""Core market data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
YEAR_SECONDS = 365.0 * 24 * 3600

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def now_ist() -> datetime:
    return datetime.now(IST)


def expiry_datetime(expiry: date) -> datetime:
    return datetime.combine(expiry, MARKET_CLOSE, tzinfo=IST)


def time_to_expiry(expiry: date, now: datetime) -> float:
    """Years until 15:30 IST on expiry day (floored at zero)."""
    return max((expiry_datetime(expiry) - now).total_seconds(), 0.0) / YEAR_SECONDS


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def is_market_open(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class OptionQuote:
    strike: float
    option_type: str  # "CE" or "PE"
    expiry: date
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    iv: float = 0.0  # annualised decimal, 0 when unknown
    oi: float = 0.0  # contracts
    oi_change: float = 0.0
    volume: float = 0.0

    @property
    def has_market(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def mid(self) -> float:
        if self.has_market:
            return (self.bid + self.ask) / 2
        return self.ltp

    @property
    def spread_pct(self) -> float | None:
        if not self.has_market or self.mid <= 0:
            return None
        return (self.ask - self.bid) / self.mid * 100


@dataclass
class OptionChain:
    symbol: str
    spot: float
    timestamp: datetime
    expiries: list[date]
    quotes: list[OptionQuote]
    lot_size: int
    _index: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._index = {(q.expiry, q.option_type, q.strike): q for q in self.quotes}

    def get(self, expiry: date, option_type: str, strike: float) -> OptionQuote | None:
        return self._index.get((expiry, option_type, float(strike)))

    def for_expiry(self, expiry: date) -> list[OptionQuote]:
        return [q for q in self.quotes if q.expiry == expiry]

    def strikes(self, expiry: date) -> list[float]:
        return sorted({q.strike for q in self.quotes if q.expiry == expiry})

    def strike_step(self, expiry: date) -> float:
        strikes = self.strikes(expiry)
        if len(strikes) < 2:
            return 0.0
        diffs = sorted(b - a for a, b in zip(strikes, strikes[1:]) if b > a)
        return diffs[len(diffs) // 2]  # median ignores odd gaps in sparse chains


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d
