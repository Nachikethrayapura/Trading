"""Market data provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Bar, OptionChain, is_market_open, now_ist


class DataUnavailable(RuntimeError):
    """Raised when a data source cannot return usable data."""


class MarketDataProvider(ABC):
    name = "base"
    simulated = False

    def advance(self) -> None:
        """Move the provider's clock forward one tick (only meaningful for simulations)."""

    def now(self) -> datetime:
        return now_ist()

    def market_open(self) -> bool:
        """Whether the cash/F&O market is trading now. Clock-based unless the source knows better."""
        return is_market_open(self.now())

    def entry_blocker(self) -> str | None:
        """A reason the provider forbids new entries right now (used by the backtest replay)."""
        return None

    @abstractmethod
    def option_chain(self, symbol: str) -> OptionChain: ...

    @abstractmethod
    def intraday_bars(self, symbol: str) -> list[Bar]:
        """Recent 5-minute bars (a few sessions)."""

    @abstractmethod
    def daily_bars(self, symbol: str) -> list[Bar]:
        """Roughly one year of daily bars."""

    @abstractmethod
    def india_vix(self) -> float | None: ...

    @abstractmethod
    def vix_history(self) -> list[float]:
        """Roughly one year of daily India VIX closes."""
