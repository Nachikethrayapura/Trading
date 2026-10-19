"""Risk manager: position sizing, portfolio limits, circuit breakers and entry gates."""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import Config
from .models import IST, parse_hhmm
from .paper import PaperBroker
from .strategies import TradeIdea


class RiskManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.halted_day: str | None = None
        self.halt_reason: str | None = None
        self.spot_history: dict[str, list[tuple[datetime, float]]] = {}
        self.last_entry: dict[str, datetime] = {}

    # ----------------------------------------------------------------- sizing
    def size(self, idea: TradeIdea) -> int:
        """Lots such that the worst case (max loss + costs) stays within risk_per_trade_pct."""
        budget = self.cfg.risk.capital * self.cfg.risk.risk_per_trade_pct / 100
        loss_per_lot = idea.max_loss * idea.lot_size + idea.costs_per_lot
        if loss_per_lot <= 0:
            return 0
        return max(0, min(int(budget // loss_per_lot), self.cfg.risk.max_lots_per_trade))

    # ----------------------------------------------------------------- daily state
    @staticmethod
    def _day_start(now: datetime) -> datetime:
        return now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)

    def daily_status(self, broker: PaperBroker, now: datetime) -> dict:
        start = self._day_start(now)
        realized = broker.realized_between(start)
        unrealized = broker.unrealized()
        limit = self.cfg.risk.capital * self.cfg.risk.max_daily_loss_pct / 100
        closed_today = [p for p in broker.closed_positions(limit=50)
                        if datetime.fromisoformat(p["exit_time"]) >= start]
        streak = 0
        for p in closed_today:  # newest first
            if (p["realized_pnl"] or 0) < 0:
                streak += 1
            else:
                break
        day = start.date().isoformat()
        if self.halted_day and self.halted_day != day:
            self.halted_day = self.halt_reason = None
        return {
            "day": day, "realized": round(realized, 2), "unrealized": round(unrealized, 2),
            "total": round(realized + unrealized, 2), "loss_limit": round(limit, 2),
            "loss_used_pct": round(max(0.0, -(realized + unrealized)) / limit * 100, 1) if limit else 0.0,
            "consecutive_losses": streak, "halted": self.halted_day == day, "halt_reason": self.halt_reason,
            "open_risk": round(broker.open_risk(), 2),
            "open_risk_limit": round(self.cfg.risk.capital * self.cfg.risk.max_portfolio_risk_pct / 100, 2),
        }

    def check_circuit_breaker(self, broker: PaperBroker, now: datetime) -> tuple[bool, bool]:
        """Returns (halted, should_flatten). Halts persist for the rest of the trading day."""
        status = self.daily_status(broker, now)
        if status["halted"]:
            return True, False
        if status["total"] <= -status["loss_limit"]:
            self.halted_day, self.halt_reason = status["day"], (
                f"Daily loss limit hit ({status['total']:,.0f} INR <= -{status['loss_limit']:,.0f})")
            return True, self.cfg.risk.flatten_on_daily_loss
        if status["consecutive_losses"] >= self.cfg.risk.max_consecutive_losses:
            self.halted_day = status["day"]
            self.halt_reason = f"{status['consecutive_losses']} consecutive losing trades today"
            return True, False
        return False, False

    # ----------------------------------------------------------------- entry gates
    def record_spot(self, symbol: str, now: datetime, spot: float) -> None:
        hist = self.spot_history.setdefault(symbol, [])
        hist.append((now, spot))
        cutoff = now - timedelta(minutes=max(self.cfg.strategy.shock_window_minutes, 1))
        while hist and hist[0][0] < cutoff:
            hist.pop(0)

    def shock_move_pct(self, symbol: str) -> float:
        hist = self.spot_history.get(symbol, [])
        if len(hist) < 2:
            return 0.0
        latest = hist[-1][1]
        return max(abs(latest / s - 1) * 100 for _, s in hist)

    def entry_blockers(self, symbol: str, now: datetime, stale: bool, simulated: bool) -> list[str]:
        s = self.cfg.strategy
        blockers = []
        local = now.astimezone(IST)
        if local.weekday() >= 5:
            blockers.append("Weekend")
        if not parse_hhmm(s.entry_start) <= local.time() <= parse_hhmm(s.entry_end):
            blockers.append(f"Outside entry window {s.entry_start}-{s.entry_end} IST")
        if local.date().isoformat() in s.event_days:
            blockers.append("Scheduled event day: no new entries")
        if stale and not simulated:
            blockers.append("Market data is stale")
        shock = self.shock_move_pct(symbol)
        if shock >= s.shock_move_pct:
            blockers.append(f"Volatility shock: {shock:.2f}% move in {s.shock_window_minutes} min")
        last = self.last_entry.get(symbol)
        if last and now - last < timedelta(minutes=s.signal_cooldown_minutes):
            blockers.append("Cooldown after the last entry")
        return blockers

    def trade_blockers(self, idea: TradeIdea, lots: int, broker: PaperBroker, now: datetime) -> list[str]:
        r = self.cfg.risk
        blockers = []
        status = self.daily_status(broker, now)
        if status["halted"]:
            blockers.append(f"Circuit breaker: {status['halt_reason']}")
        if lots <= 0:
            blockers.append("Max loss of one lot exceeds the per-trade risk budget")
        if len(broker.open_positions()) >= r.max_open_positions:
            blockers.append(f"Max open positions ({r.max_open_positions}) reached")
        if r.one_position_per_symbol and broker.open_positions(idea.symbol):
            blockers.append(f"Already holding a {idea.symbol} position")
        new_risk = idea.max_loss * idea.lot_size * lots
        if status["open_risk"] + new_risk > status["open_risk_limit"]:
            blockers.append("Portfolio risk limit would be exceeded")
        return blockers
