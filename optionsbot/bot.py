"""Orchestrator: polls data, runs analytics and strategies, manages paper trades, publishes state."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time as _time
from collections import deque
from datetime import date, datetime
from pathlib import Path

from .analytics.chain_metrics import analyze_chain, choose_expiry
from .analytics.indicators import clamp, realized_vol, trend_score
from .config import Config
from .data.base import DataUnavailable, MarketDataProvider
from .models import INDEX_SYMBOLS, Bar, OptionChain
from .notify import make_notifier
from .paper import PaperBroker
from .risk import RiskManager
from .storage import Store
from .strategies import MarketView, TradeIdea, generate_ideas

log = logging.getLogger(__name__)

INTRADAY_TTL = 240.0
DAILY_TTL = 6 * 3600.0
AFTER_HOURS_POLL = 300.0
RECORD_HEAVY_KEYS = ("equity_curve", "closed", "events", "stats")
ALERT_ICONS = {"entry": "🟢 ENTRY", "exit": "🔵 EXIT", "risk": "⛔ RISK"}


def make_provider(cfg: Config) -> MarketDataProvider:
    if cfg.data.provider == "simulated":
        from .data.simulated import SimulatedProvider
        return SimulatedProvider(cfg)
    if cfg.data.provider == "nse":
        from .data.nse import NSEProvider
        return NSEProvider(cfg)
    if cfg.data.provider == "upstox":
        from .data.brokers import UpstoxProvider
        return UpstoxProvider(cfg)
    if cfg.data.provider == "kite":
        from .data.brokers import KiteProvider
        return KiteProvider(cfg)
    raise ValueError(f"Unknown data provider {cfg.data.provider!r} (use nse, upstox, kite or simulated)")


def percentile_rank(history: list[float], value: float) -> float | None:
    if len(history) < 20:
        return None
    lo, hi = min(history), max(history)
    return 50.0 if hi == lo else clamp((value - lo) / (hi - lo) * 100, 0, 100)


def performance(closed: list[dict], curve: list[dict]) -> dict:
    pnls = [p["realized_pnl"] or 0.0 for p in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    peak, max_dd = -math.inf, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        max_dd = max(max_dd, peak - point["equity"])
    return {
        "trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "total_pnl": sum(pnls), "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "expectancy": sum(pnls) / len(pnls) if pnls else None, "max_drawdown": max_dd,
    }


def _clean(obj):
    """Make the state JSON-safe (no NaN/inf, dates as strings)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


class TradingBot:
    def __init__(self, cfg: Config, provider: MarketDataProvider | None = None, store: Store | None = None,
                 alerts: bool = True, record_path: str | None = None) -> None:
        self.cfg = cfg
        self.record_path = record_path
        self.notifier = make_notifier(cfg.alerts) if alerts else None
        self._was_halted = False
        self._market_open = False
        self.provider = provider or make_provider(cfg)
        self.store = store or Store(cfg.db_path)
        self.broker = PaperBroker(cfg, self.store)
        self.risk = RiskManager(cfg)
        self.paused = False
        self.auto_paper = cfg.auto_paper_trade
        self.state: dict = {}
        self.ideas: dict[str, TradeIdea] = {}
        self.chains: dict[str, OptionChain] = {}
        self.events: deque[dict] = deque(maxlen=200)
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.RLock()
        self._subscribers: set[asyncio.Queue] = set()
        self._stopping = False

    # ----------------------------------------------------------------- helpers
    def _event(self, now: datetime, kind: str, message: str) -> None:
        self.events.appendleft({"time": now.isoformat(), "kind": kind, "message": message})
        log.info("[%s] %s", kind, message)
        if self.notifier and kind in ALERT_ICONS:
            self.notifier.send(f"{ALERT_ICONS[kind]} {message}")

    def _alert(self, text: str, key: str, cooldown_minutes: float) -> None:
        if self.notifier:
            self.notifier.send(text, key=key, cooldown=cooldown_minutes * 60)

    def _cached(self, key: tuple, ttl: float, fetch):
        if self.provider.simulated:
            return fetch()
        hit = self._cache.get(key)
        if hit and _time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            value = fetch()
        except DataUnavailable:
            if hit:  # stale history beats none for slow-moving inputs
                return hit[1]
            raise
        self._cache[key] = (_time.monotonic(), value)
        return value

    def _iv_rank(self, symbol: str, atm_iv: float | None, vix: float | None,
                 vix_hist: list[float]) -> tuple[float | None, str]:
        if symbol in INDEX_SYMBOLS and vix is not None:
            rank = percentile_rank(vix_hist, vix)
            if rank is not None:
                return rank, "India VIX, 1y range"
        if atm_iv is not None:
            rank = percentile_rank(self.store.iv_history(symbol), atm_iv)
            if rank is not None:
                return rank, "own ATM IV history"
        return None, "insufficient history"

    # ----------------------------------------------------------------- cycle
    def step(self) -> dict:
        with self._lock:
            self.provider.advance()
            now = self.provider.now()
            self._market_open = self.provider.market_open()
            errors: list[str] = []
            try:
                vix = self._cached(("vix",), INTRADAY_TTL, self.provider.india_vix)
                vix_hist = self._cached(("vix_hist",), DAILY_TTL, self.provider.vix_history)
            except DataUnavailable as exc:
                vix, vix_hist = None, []
                errors.append(f"India VIX: {exc}")

            symbols: dict[str, dict] = {}
            for symbol in self.cfg.symbols:
                try:
                    symbols[symbol] = self._process_symbol(symbol, now, vix, vix_hist)
                except Exception as exc:  # one bad symbol must not stop the others
                    if not isinstance(exc, DataUnavailable):
                        log.exception("Error processing %s", symbol)
                    errors.append(f"{symbol}: {exc}")
                    symbols[symbol] = {"error": str(exc)}
                    self._alert(f"⚠️ DATA {symbol}: {exc}", f"err:{symbol}", self.cfg.alerts.data_error_cooldown_minutes)

            halted, flatten = self.risk.check_circuit_breaker(self.broker, now)
            if flatten and self.broker.open_positions():
                self._event(now, "risk", f"Circuit breaker: {self.risk.halt_reason}. Flattening all positions")
                self._flatten(now, "Circuit breaker: daily loss limit")
            elif halted and not self._was_halted:
                self._event(now, "risk", f"Circuit breaker: {self.risk.halt_reason}. No new entries today")
            self._was_halted = halted

            realized = self.broker.total_realized()
            unrealized = self.broker.unrealized()
            self.store.record_equity(now.isoformat(), self.cfg.risk.capital + realized + unrealized,
                                     realized, unrealized)
            self.state = _clean(self._snapshot(now, symbols, errors, vix))
            if self.record_path:
                self._record(self.state)
            return self.state

    def _process_symbol(self, symbol: str, now: datetime, vix: float | None, vix_hist: list[float]) -> dict:
        cfg = self.cfg
        chain = self.provider.option_chain(symbol)
        self.chains[symbol] = chain
        age = (now - chain.timestamp).total_seconds()
        stale = age > cfg.data.stale_after_seconds
        self.risk.record_spot(symbol, now, chain.spot)

        for pos in self.broker.update(chain, now):
            self._event(now, "exit", f"{pos.symbol} {pos.strategy} closed: {pos.exit_reason}. "
                                     f"P&L {pos.realized_pnl:+,.0f} INR")

        expiry = choose_expiry(chain, now, cfg.strategy.min_dte, cfg.strategy.max_dte)
        if expiry is None:
            raise DataUnavailable(f"no expiry between {cfg.strategy.min_dte} and {cfg.strategy.max_dte} days")
        analytics = analyze_chain(chain, expiry, now, cfg.risk_free_rate)

        intraday: list[Bar] = self._cached((symbol, "5m"), INTRADAY_TTL, lambda: self.provider.intraday_bars(symbol))
        daily: list[Bar] = self._cached((symbol, "1d"), DAILY_TTL, lambda: self.provider.daily_bars(symbol))
        trend, components = trend_score(intraday, daily)
        rv = realized_vol([b.close for b in daily[-21:]])
        if analytics.atm_iv:
            self.store.record_iv(symbol, now.date().isoformat(), analytics.atm_iv)
        iv_rank, iv_source = self._iv_rank(symbol, analytics.atm_iv, vix, vix_hist)
        view = MarketView(analytics, trend, components, clamp(0.65 * trend + 0.35 * analytics.oi_sentiment),
                          iv_rank, iv_source, rv, vix)

        regime, ideas = generate_ideas(view, chain, now, cfg)
        common = self.risk.entry_blockers(symbol, now, stale, self.provider.simulated)
        if (provider_block := self.provider.entry_blocker()):
            common.append(provider_block)
        if not self.provider.simulated and not self._market_open and "Weekend" not in common:
            common.append("Market closed (exchange holiday or outside trading hours)")
        for idea in ideas:
            idea.lots = self.risk.size(idea)
            idea.blockers = common + self.risk.trade_blockers(idea, idea.lots, self.broker, now)
            idea.status = "blocked" if idea.blockers else "actionable"
        if ideas and ideas[0].status == "actionable":
            if self.paused:
                ideas[0].blockers.append("Bot paused")
                ideas[0].status = "blocked"
            elif self.auto_paper:
                self._open(ideas[0], chain, now)
            elif self.cfg.alerts.recommendations:
                top = ideas[0].to_dict()
                legs = " / ".join(l.label for l in ideas[0].legs)
                self._alert(
                    f"💡 IDEA {symbol} {ideas[0].strategy} exp {ideas[0].expiry}: {legs}. "
                    f"{'Credit' if top['net_premium_per_lot'] >= 0 else 'Debit'} ₹{abs(top['net_premium_per_lot']):,.0f}/lot, "
                    f"max loss ₹{top['max_loss_per_lot']:,.0f}/lot, POP {ideas[0].pop:.0%}, "
                    f"confidence {ideas[0].confidence:.0%}, size {ideas[0].lots} lot(s)",
                    f"idea:{symbol}:{ideas[0].strategy}:{legs}", self.cfg.alerts.recommendation_cooldown_minutes)
        for idea_id in [k for k, v in self.ideas.items() if v.symbol == symbol]:
            del self.ideas[idea_id]
        self.ideas.update({i.id: i for i in ideas})

        return {
            "analytics": analytics.to_dict(), "view": view.to_dict(), "regime": regime,
            "ideas": [i.to_dict() for i in ideas], "stale": stale, "data_age_seconds": age,
        }

    def _record(self, state: dict) -> None:
        """Append a light frame per tick; keep the latest full state beside it (for `optionsbot.demo`)."""
        frame = {k: v for k, v in state.items() if k not in RECORD_HEAVY_KEYS}
        with open(self.record_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(frame, separators=(",", ":")) + "\n")
        final = Path(f"{self.record_path}.final.json")
        tmp = final.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":")))
        tmp.replace(final)

    def _open(self, idea: TradeIdea, chain: OptionChain, now: datetime) -> None:
        pos = self.broker.open(idea, chain, now, idea.lots)
        idea.status, idea.position_id = "paper_traded", pos.id
        self.risk.last_entry[idea.symbol] = now
        premium = -pos.entry_value * pos.quantity
        self._event(now, "entry", f"Paper {idea.symbol} {idea.strategy} x{pos.lots} lots "
                                  f"({' / '.join(l.label for l in idea.legs)}), "
                                  f"{'credit' if premium > 0 else 'debit'} {abs(premium):,.0f} INR, "
                                  f"max loss {pos.risk_inr:,.0f} INR")

    def _flatten(self, now: datetime, reason: str) -> None:
        for pos in self.broker.open_positions():
            chain = self.chains.get(pos.symbol)
            if chain is None:
                continue
            closed = self.broker.close(pos.id, chain, now, reason)
            self._event(now, "exit", f"{closed.symbol} {closed.strategy} closed: {reason}. "
                                     f"P&L {closed.realized_pnl:+,.0f} INR")

    def _snapshot(self, now: datetime, symbols: dict, errors: list[str], vix: float | None) -> dict:
        closed = self.broker.closed_positions(limit=500)
        curve = self.store.equity_curve(limit=1500)
        risk = self.risk.daily_status(self.broker, now)
        realized = self.broker.total_realized()
        risk.update(capital=self.cfg.risk.capital,
                    equity=self.cfg.risk.capital + realized + self.broker.unrealized(),
                    risk_per_trade=self.cfg.risk.capital * self.cfg.risk.risk_per_trade_pct / 100,
                    max_open_positions=self.cfg.risk.max_open_positions)
        return {
            "time": now, "provider": self.provider.name, "simulated": self.provider.simulated,
            "market_open": self._market_open, "paused": self.paused, "auto_paper": self.auto_paper,
            "vix": vix, "symbols": symbols, "errors": errors, "risk": risk,
            "positions": [p.to_dict() for p in self.broker.open_positions()],
            "closed": closed[:50], "stats": performance(closed, curve),
            "equity_curve": curve, "events": list(self.events),
        }

    # ----------------------------------------------------------------- controls
    def paper_trade(self, idea_id: str) -> dict:
        with self._lock:
            idea = self.ideas.get(idea_id)
            if idea is None:
                raise KeyError("Recommendation expired; wait for the next refresh")
            chain = self.chains[idea.symbol]
            now = self.provider.now()
            if idea.status == "paper_traded":
                raise ValueError("Already paper traded")
            blockers = self.risk.trade_blockers(idea, idea.lots, self.broker, now)
            if blockers:
                raise ValueError("; ".join(blockers))
            self._open(idea, chain, now)
            return {"position_id": idea.position_id}

    def close_position(self, pos_id: str) -> dict:
        with self._lock:
            pos = next((p for p in self.broker.open_positions() if p.id == pos_id), None)
            if pos is None:
                raise KeyError("Position not found")
            now = self.provider.now()
            closed = self.broker.close(pos_id, self.chains[pos.symbol], now, "Closed manually")
            self._event(now, "exit", f"{closed.symbol} {closed.strategy} closed manually. "
                                     f"P&L {closed.realized_pnl:+,.0f} INR")
            return closed.to_dict()

    def flatten(self) -> None:
        with self._lock:
            self._flatten(self.provider.now(), "Flattened manually")

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._event(self.provider.now(), "control", "Bot paused" if paused else "Bot resumed")

    def set_auto_paper(self, enabled: bool) -> None:
        self.auto_paper = enabled
        self._event(self.provider.now(), "control", f"Auto paper trading {'on' if enabled else 'off'}")

    # ----------------------------------------------------------------- async loop
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(self.state)

    async def run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.to_thread(self.step)
                self.publish()
            except Exception:
                log.exception("Bot cycle failed")
            delay = self.cfg.data.poll_seconds
            if not self.provider.simulated and not self._market_open:
                delay = max(delay, AFTER_HOURS_POLL)
            await asyncio.sleep(delay)

    def stop(self) -> None:
        self._stopping = True
