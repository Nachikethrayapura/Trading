"""Paper broker: simulated fills, mark-to-market and rule-based exits."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from .analytics.greeks import bs_greeks, bs_price, intrinsic
from .config import Config
from .costs import fill_price, order_costs
from .models import OptionChain, expiry_datetime, parse_hhmm, time_to_expiry
from .storage import Store
from .strategies import CREDIT, TradeIdea


@dataclass
class Position:
    id: str
    idea_id: str
    symbol: str
    strategy: str
    kind: str
    bias: str
    expiry: str
    legs: list[dict]  # option_type, strike, side, entry_price, iv, current_price, delta
    lots: int
    lot_size: int
    entry_time: str
    entry_value: float  # per unit, from fills
    max_profit: float
    max_loss: float
    take_profit: float
    stop_loss: float
    entry_costs: float
    status: str = "open"
    current_value: float = 0.0
    unrealized_pnl: float = 0.0  # INR, net of entry costs
    max_short_delta: float = 0.0
    exit_time: str | None = None
    exit_value: float | None = None
    exit_reason: str | None = None
    exit_costs: float = 0.0
    realized_pnl: float | None = None
    updated_at: str | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def pnl_per_unit(self) -> float:
        return self.current_value - self.entry_value

    @property
    def risk_inr(self) -> float:
        return self.max_loss * self.quantity

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(quantity=self.quantity, pnl_per_unit=round(self.pnl_per_unit, 2), risk_inr=round(self.risk_inr, 2))
        return d


class PaperBroker:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.positions: dict[str, Position] = {}
        for data in store.load_positions(status="open"):
            data = {k: v for k, v in data.items() if k in Position.__dataclass_fields__}
            pos = Position(**data)
            self.positions[pos.id] = pos

    # ----------------------------------------------------------------- queries
    def open_positions(self, symbol: str | None = None) -> list[Position]:
        return [p for p in self.positions.values() if symbol is None or p.symbol == symbol]

    def closed_positions(self, limit: int = 200) -> list[dict]:
        return self.store.load_positions(status="closed", limit=limit)

    def realized_between(self, start: datetime, end: datetime | None = None) -> float:
        total = 0.0
        for p in self.store.load_positions(status="closed"):
            ts = datetime.fromisoformat(p["exit_time"])
            if ts >= start and (end is None or ts <= end):
                total += p["realized_pnl"] or 0.0
        return total

    def total_realized(self) -> float:
        return sum(p["realized_pnl"] or 0.0 for p in self.store.load_positions(status="closed"))

    def unrealized(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def open_risk(self) -> float:
        return sum(p.risk_inr for p in self.positions.values())

    # ----------------------------------------------------------------- pricing
    def _leg_mark(self, leg: dict, chain: OptionChain, now: datetime) -> tuple[float, float, float, float]:
        """(mid, bid, ask, delta) for a leg, falling back to Black-Scholes when the quote is missing."""
        expiry = date.fromisoformat(leg["expiry"])
        t = time_to_expiry(expiry, now)
        q = chain.get(expiry, leg["option_type"], leg["strike"])
        iv = q.iv if q and q.iv > 0 else leg["iv"]
        delta = bs_greeks(chain.spot, leg["strike"], t, self.cfg.risk_free_rate, iv, leg["option_type"]).delta
        if q and q.mid > 0:
            return q.mid, q.bid, q.ask, delta
        theo = bs_price(chain.spot, leg["strike"], t, self.cfg.risk_free_rate, iv, leg["option_type"])
        return theo, 0.0, 0.0, delta

    # ----------------------------------------------------------------- trading
    def open(self, idea: TradeIdea, chain: OptionChain, now: datetime, lots: int) -> Position:
        fills, legs = [], []
        qty = lots * idea.lot_size
        for leg in idea.legs:
            ld = {"option_type": leg.option_type, "strike": leg.strike, "expiry": leg.expiry,
                  "side": leg.side, "iv": leg.iv}
            mid, bid, ask, delta = self._leg_mark(ld, chain, now)
            price = fill_price(leg.side, mid, bid, ask, self.cfg.costs)
            ld.update(entry_price=price, current_price=round(mid, 2), delta=delta)
            legs.append(ld)
            fills.append((leg.side, price, qty))
        entry_value = sum(l["side"] * l["entry_price"] for l in legs)
        slip = entry_value - idea.entry_value  # fills are worse than the idea's mid prices
        pos = Position(
            id=uuid.uuid4().hex[:10], idea_id=idea.id, symbol=idea.symbol, strategy=idea.strategy,
            kind=idea.kind, bias=idea.bias, expiry=idea.expiry, legs=legs, lots=lots,
            lot_size=idea.lot_size, entry_time=now.isoformat(), entry_value=round(entry_value, 2),
            max_profit=round(idea.max_profit - slip, 2), max_loss=round(idea.max_loss + slip, 2),
            take_profit=idea.take_profit, stop_loss=idea.stop_loss,
            entry_costs=round(order_costs(fills, self.cfg.costs), 2),
        )
        self._remark(pos, chain, now)
        self.positions[pos.id] = pos
        self.store.save_position(pos.to_dict())
        return pos

    def _remark(self, pos: Position, chain: OptionChain, now: datetime) -> None:
        value, short_delta = 0.0, 0.0
        for leg in pos.legs:
            mid, _, _, delta = self._leg_mark(leg, chain, now)
            leg["current_price"], leg["delta"] = round(mid, 2), delta
            value += leg["side"] * mid
            if leg["side"] < 0:
                short_delta = max(short_delta, abs(delta))
        pos.current_value = round(value, 2)
        pos.max_short_delta = round(short_delta, 3)
        pos.unrealized_pnl = round(pos.pnl_per_unit * pos.quantity - pos.entry_costs, 2)
        pos.updated_at = now.isoformat()

    def close(self, pos_id: str, chain: OptionChain | None, now: datetime, reason: str,
              settle_spot: float | None = None) -> Position:
        pos = self.positions.pop(pos_id)
        fills, exit_value = [], 0.0
        for leg in pos.legs:
            if settle_spot is not None:  # cash-settled at expiry: intrinsic value, no slippage
                price = intrinsic(settle_spot, leg["strike"], leg["option_type"])
            else:
                mid, bid, ask, _ = self._leg_mark(leg, chain, now)
                price = fill_price(-leg["side"], mid, bid, ask, self.cfg.costs) if mid > 0 else 0.0
                fills.append((-leg["side"], price, pos.quantity))
            leg["exit_price"] = round(price, 2)
            exit_value += leg["side"] * price
        pos.status = "closed"
        pos.exit_time = now.isoformat()
        pos.exit_value = round(exit_value, 2)
        pos.exit_reason = reason
        pos.exit_costs = round(order_costs(fills, self.cfg.costs), 2) if fills else 0.0
        pos.current_value = pos.exit_value
        pos.realized_pnl = round((exit_value - pos.entry_value) * pos.quantity - pos.entry_costs - pos.exit_costs, 2)
        pos.unrealized_pnl = 0.0
        self.store.save_position(pos.to_dict())
        return pos

    def update(self, chain: OptionChain, now: datetime) -> list[Position]:
        """Mark the symbol's positions to market and apply exit rules. Returns closed positions."""
        closed = []
        exit_time = parse_hhmm(self.cfg.exits.expiry_day_exit_time)
        for pos in self.open_positions(chain.symbol):
            expiry = date.fromisoformat(pos.expiry)
            if now >= expiry_datetime(expiry):
                closed.append(self.close(pos.id, None, now, "Expired: cash-settled at intrinsic",
                                         settle_spot=chain.spot))
                continue
            self._remark(pos, chain, now)
            pnl = pos.pnl_per_unit
            reason = None
            if pnl >= pos.take_profit:
                reason = f"Take profit: {pnl:+.2f}/unit reached target {pos.take_profit:+.2f}"
            elif pnl <= pos.stop_loss:
                reason = f"Stop loss: {pnl:+.2f}/unit breached stop {pos.stop_loss:+.2f}"
            elif pos.kind == CREDIT and pos.max_short_delta >= self.cfg.exits.short_delta_exit:
                reason = f"Short strike under pressure: delta {pos.max_short_delta:.2f}"
            elif now.date() == expiry and now.time() >= exit_time:
                reason = "Time exit: avoiding expiry-day gamma"
            if reason:
                closed.append(self.close(pos.id, chain, now, reason))
            else:
                self.store.save_position(pos.to_dict())
        return closed
