"""Strategy engine: reads the market regime, builds defined-risk option structures and scores them.

The approach mirrors how volatility desks trade index options:
  * Volatility risk premium: sell premium when implied vol is rich versus realised vol.
  * Dealer gamma: positive net GEX pins price (range trades); negative GEX lets it trend.
  * OI positioning: short strikes sit beyond the heaviest call/put writing (OI walls).
  * Trend: buy defined-risk debit spreads only when vol is cheap and the trend is strong.
Every structure has a capped maximum loss; naked short options are never recommended.
Candidates are scored on expected value under a fat-tailed *physical* distribution
(realised/implied vol blend plus a crash regime), not the risk-neutral one, so a trade
only qualifies when it carries an edge after costs.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import numpy as np

from .analytics.chain_metrics import ChainAnalytics
from .analytics.greeks import bs_greeks
from .analytics.indicators import clamp
from .config import Config
from .costs import round_trip_costs
from .models import OptionChain, OptionQuote

CREDIT, DEBIT = "credit", "debit"
STRATEGY_KIND = {
    "IRON_CONDOR": CREDIT,
    "BULL_PUT_SPREAD": CREDIT,
    "BEAR_CALL_SPREAD": CREDIT,
    "BULL_CALL_SPREAD": DEBIT,
    "BEAR_PUT_SPREAD": DEBIT,
}
STRATEGY_BIAS = {
    "IRON_CONDOR": "NEUTRAL",
    "BULL_PUT_SPREAD": "BULLISH",
    "BEAR_CALL_SPREAD": "BEARISH",
    "BULL_CALL_SPREAD": "BULLISH",
    "BEAR_PUT_SPREAD": "BEARISH",
}

_Z = np.linspace(-6.0, 6.0, 1201)
_ZW = np.exp(-0.5 * _Z ** 2)
_ZW /= _ZW.sum()


@dataclass
class Leg:
    option_type: str
    strike: float
    expiry: str
    side: int  # +1 buy, -1 sell
    price: float
    iv: float
    delta: float

    @property
    def label(self) -> str:
        return f"{'BUY' if self.side > 0 else 'SELL'} {self.strike:g} {self.option_type}"

    def to_dict(self) -> dict:
        return {**asdict(self), "label": self.label}


@dataclass
class MarketView:
    analytics: ChainAnalytics
    trend: float
    trend_components: dict[str, float]
    direction: float  # blended trend + OI sentiment in [-1, 1]
    iv_rank: float | None
    iv_rank_source: str
    realized_vol: float | None
    vix: float | None

    @property
    def vrp(self) -> float | None:
        if self.analytics.atm_iv is None or self.realized_vol is None:
            return None
        return self.analytics.atm_iv - self.realized_vol

    def to_dict(self) -> dict:
        return {
            "trend": self.trend, "trend_components": self.trend_components,
            "direction": self.direction, "iv_rank": self.iv_rank,
            "iv_rank_source": self.iv_rank_source, "realized_vol": self.realized_vol,
            "vrp": self.vrp, "vix": self.vix,
        }


@dataclass
class TradeIdea:
    id: str
    symbol: str
    strategy: str
    kind: str
    bias: str
    expiry: str
    legs: list[Leg]
    lot_size: int
    entry_value: float  # per unit: sum(side * price); negative means net credit
    max_profit: float  # per unit
    max_loss: float  # per unit, positive
    breakevens: list[float]
    pop: float
    ev: float  # per unit, after round-trip costs
    ev_per_risk: float
    costs_per_lot: float
    confidence: float
    take_profit: float  # per-unit P&L that triggers profit taking
    stop_loss: float  # per-unit P&L (negative) that triggers the stop
    reasons: list[str]
    created_at: str
    lots: int = 0
    status: str = "new"
    blockers: list[str] = field(default_factory=list)
    position_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [leg.to_dict() for leg in self.legs]
        per_lot = self.lot_size
        d.update(
            net_premium_per_lot=-self.entry_value * per_lot,
            max_profit_per_lot=self.max_profit * per_lot,
            max_loss_per_lot=self.max_loss * per_lot,
            ev_per_lot=self.ev * per_lot,
        )
        return d


@dataclass
class _Ctx:
    spot: float
    t: float
    r: float
    phys_vol: float
    shift: float  # expected log-drift over the horizon from the directional view
    cfg: Config
    lot_size: int
    oi_available: bool = True
    terminal: np.ndarray = field(default=None)  # type: ignore[assignment]
    weights: np.ndarray = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        s = self.cfg.strategy
        prices, weights = [], []
        for prob, vol in ((1 - s.tail_prob, self.phys_vol), (s.tail_prob, self.phys_vol * s.tail_vol_mult)):
            if prob <= 0:
                continue
            sd = vol * np.sqrt(self.t)
            prices.append(self.spot * np.exp(self.shift + sd * _Z - 0.5 * sd * sd))
            weights.append(_ZW * prob)
        self.terminal = np.concatenate(prices)
        self.weights = np.concatenate(weights)


def payoff(legs: list[Leg], spot: np.ndarray | float) -> np.ndarray:
    s = np.asarray(spot, dtype=float)
    total = np.zeros_like(s)
    for leg in legs:
        intrinsic = np.maximum(s - leg.strike, 0) if leg.option_type == "CE" else np.maximum(leg.strike - s, 0)
        total = total + leg.side * intrinsic
    return total


def evaluate(legs: list[Leg], ctx: _Ctx) -> dict | None:
    """At-expiry P&L statistics per unit of the structure, net of estimated costs."""
    entry_value = sum(leg.side * leg.price for leg in legs)
    costs = round_trip_costs([(leg.side, leg.price) for leg in legs], ctx.lot_size, ctx.cfg.costs) / ctx.lot_size
    strikes = sorted({leg.strike for leg in legs})
    probe = np.array([strikes[0] * 0.5, *strikes, strikes[-1] * 1.5, strikes[-1] * 1.6])
    pnl_probe = payoff(legs, probe) - entry_value
    if abs(pnl_probe[-1] - pnl_probe[-2]) > 1e-9 or abs(payoff(legs, 1.0)[()] - payoff(legs, strikes[0] * 0.5)[()]) > 1e-6:
        return None  # unbounded risk: never allowed
    max_profit, max_loss = float(pnl_probe.max()), float(-pnl_probe.min())
    if max_loss <= 0 or max_profit <= 0:
        return None
    net = payoff(legs, ctx.terminal) - entry_value - costs
    ev = float(np.dot(net, ctx.weights))
    pop = float(ctx.weights[net > 0].sum())

    grid = np.linspace(strikes[0] * 0.9, strikes[-1] * 1.1, 4001)
    g = payoff(legs, grid) - entry_value
    idx = np.nonzero(np.diff(np.sign(g)))[0]
    breakevens = [float(grid[i] - g[i] * (grid[i + 1] - grid[i]) / (g[i + 1] - g[i])) for i in idx]
    return {
        "entry_value": entry_value, "costs": costs, "max_profit": max_profit, "max_loss": max_loss,
        "ev": ev, "pop": pop, "ev_per_risk": ev / max_loss, "breakevens": breakevens,
    }


def _liquid(q: OptionQuote | None, ctx: _Ctx, min_oi: float | None = None) -> bool:
    if q is None or q.mid <= 0 or q.iv <= 0:
        return False
    s = ctx.cfg.strategy
    if ctx.oi_available and q.oi < (s.min_oi if min_oi is None else min_oi):
        return False
    spread = q.spread_pct
    return spread is None or spread <= s.max_bid_ask_pct


def _leg(q: OptionQuote, side: int, ctx: _Ctx) -> Leg:
    g = bs_greeks(ctx.spot, q.strike, ctx.t, ctx.r, q.iv, q.option_type)
    return Leg(q.option_type, q.strike, q.expiry.isoformat(), side, round(q.mid, 2), q.iv, g.delta)


def _credit_verticals(chain: OptionChain, expiry: date, option_type: str, step: float,
                      ctx: _Ctx) -> list[tuple[list[Leg], dict]]:
    s = ctx.cfg.strategy
    direction = -1 if option_type == "PE" else 1  # wings go further out of the money
    out = []
    for q in chain.for_expiry(expiry):
        if q.option_type != option_type or (q.strike - ctx.spot) * direction <= 0:
            continue
        if not _liquid(q, ctx):
            continue
        short = _leg(q, -1, ctx)
        if not s.short_delta_min <= abs(short.delta) <= s.short_delta_max:
            continue
        for w in range(1, s.max_wing_steps + 1):
            lq = chain.get(expiry, option_type, q.strike + direction * w * step)
            if not _liquid(lq, ctx, min_oi=s.min_oi / 2):
                continue
            legs = [short, _leg(lq, +1, ctx)]
            credit = short.price - legs[1].price
            if credit <= 0 or credit / (w * step) < s.min_credit_to_width:
                continue
            stats = evaluate(legs, ctx)
            if stats:
                out.append((legs, stats))
    return out


def _debit_verticals(chain: OptionChain, expiry: date, option_type: str, step: float,
                     atm: float, ctx: _Ctx) -> list[tuple[list[Leg], dict]]:
    s = ctx.cfg.strategy
    direction = 1 if option_type == "CE" else -1  # short leg sits further in the trend direction
    out = []
    for offset in (-1, 0, 1):
        lq = chain.get(expiry, option_type, atm + direction * offset * step)
        if not _liquid(lq, ctx):
            continue
        for w in range(2, 2 * s.max_wing_steps + 1):
            sq = chain.get(expiry, option_type, lq.strike + direction * w * step)
            if not _liquid(sq, ctx, min_oi=s.min_oi / 2):
                continue
            legs = [_leg(lq, +1, ctx), _leg(sq, -1, ctx)]
            debit = legs[0].price - legs[1].price
            if debit <= 0 or debit / (w * step) > s.max_debit_to_width:
                continue
            stats = evaluate(legs, ctx)
            if stats:
                out.append((legs, stats))
    return out


def decide_regime(view: MarketView, cfg: Config) -> dict:
    s = cfg.strategy
    a = view.analytics
    d = view.direction
    vrp = view.vrp
    rich = (view.iv_rank is not None and view.iv_rank >= s.iv_rich_rank) or (vrp is not None and vrp >= 0.02)
    cheap = (view.iv_rank is None or view.iv_rank <= s.iv_cheap_rank) and (vrp is None or vrp <= 0.01)
    allowed: list[str] = []
    notes: list[str] = []
    if rich:
        notes.append("Implied vol is rich: favour selling defined-risk premium")
        if abs(d) < s.trend_threshold:
            if a.gex_regime == "positive":
                allowed.append("IRON_CONDOR")
                notes.append("Positive dealer gamma + no trend: range-bound, iron condor")
            elif a.gex_regime == "unknown":
                allowed.append("IRON_CONDOR")
                notes.append("No trend (dealer gamma unknown without OI data): iron condor")
            else:
                notes.append("Negative dealer gamma without a clear trend: ranges unreliable, stand aside")
        elif d > 0:
            allowed.append("BULL_PUT_SPREAD")
        else:
            allowed.append("BEAR_CALL_SPREAD")
    elif abs(d) >= s.strong_trend_threshold:
        allowed.append("BULL_CALL_SPREAD" if d > 0 else "BEAR_PUT_SPREAD")
        notes.append("Vol not rich + strong trend: buy a defined-risk debit spread with the trend")
    else:
        notes.append("No edge: vol is not rich and the trend is not strong. Standing aside")
    disabled = [a for a in allowed if a not in s.enabled_strategies]
    if disabled:
        notes.append(f"{', '.join(disabled)} disabled in config")
        allowed = [a for a in allowed if a in s.enabled_strategies]
    label = ("PREMIUM_SELLING" if rich else "TREND_FOLLOWING") if allowed else "NO_TRADE"
    return {"label": label, "allowed": allowed, "notes": notes, "vol_rich": rich, "vol_cheap": cheap}


def _vol_richness(view: MarketView) -> float:
    rank = clamp(((view.iv_rank or 0) - 20) / 50, 0, 1)
    vrp = clamp((view.vrp or 0) / 0.04, 0, 1)
    return 0.5 * rank + 0.5 * vrp


def _confidence(strategy: str, view: MarketView, stats: dict, wall_protected: bool, cfg: Config) -> float:
    s = cfg.strategy
    d = abs(view.direction)
    rich = _vol_richness(view)
    gex_pos = {"positive": 1.0, "negative": 0.0}.get(view.analytics.gex_regime, 0.5)
    if strategy == "IRON_CONDOR":
        regime = 0.45 * rich + 0.35 * clamp(1 - d / s.trend_threshold, 0, 1) + 0.2 * gex_pos
    elif STRATEGY_KIND[strategy] == CREDIT:
        regime = 0.4 * rich + 0.4 * clamp(d / s.strong_trend_threshold, 0, 1) + 0.2 * float(wall_protected)
    else:
        regime = 0.5 * clamp(d / 0.8, 0, 1) + 0.25 * (1 - rich) + 0.25 * (1 - gex_pos)
    return round(0.7 * regime + 0.3 * clamp(stats["ev_per_risk"] / 0.15, 0, 1), 3)


def _wall_protected(legs: list[Leg], a: ChainAnalytics) -> bool:
    shorts = [leg for leg in legs if leg.side < 0]
    for leg in shorts:
        if leg.option_type == "PE" and (a.put_wall is None or leg.strike > a.put_wall):
            return False
        if leg.option_type == "CE" and (a.call_wall is None or leg.strike < a.call_wall):
            return False
    return bool(shorts)


def _exit_levels(kind: str, stats: dict, cfg: Config) -> tuple[float, float]:
    e = cfg.exits
    if kind == CREDIT:
        credit = -stats["entry_value"]
        take = stats["max_profit"] * e.credit_take_profit_pct / 100
        stop = -min(stats["max_loss"], credit * e.credit_stop_loss_multiple)
    else:
        debit = stats["entry_value"]
        take = stats["max_profit"] * e.debit_take_profit_pct / 100
        stop = -min(stats["max_loss"], debit * e.debit_stop_loss_pct / 100)
    return round(take, 2), round(stop, 2)


def generate_ideas(view: MarketView, chain: OptionChain, now: datetime, cfg: Config) -> tuple[dict, list[TradeIdea]]:
    a = view.analytics
    regime = decide_regime(view, cfg)
    if a.atm_iv is None or a.t_years <= 0 or not regime["allowed"]:
        if a.atm_iv is None:
            regime["notes"].append("No implied-vol data for the selected expiry")
        return regime, []

    s = cfg.strategy
    w = s.vrp_weight if view.realized_vol is not None else 0.0
    phys_vol = w * (view.realized_vol or 0) + (1 - w) * a.atm_iv
    shift = s.trend_edge * view.direction * phys_vol * np.sqrt(a.t_years)
    ctx = _Ctx(a.spot, a.t_years, cfg.risk_free_rate, phys_vol, float(shift), cfg, a.lot_size, chain.oi_available)
    expiry = date.fromisoformat(a.expiry)
    step = a.strike_step

    candidates: list[tuple[str, list[Leg], dict]] = []
    for strategy in regime["allowed"]:
        if strategy == "BULL_PUT_SPREAD":
            candidates += [(strategy, l, st) for l, st in _credit_verticals(chain, expiry, "PE", step, ctx)]
        elif strategy == "BEAR_CALL_SPREAD":
            candidates += [(strategy, l, st) for l, st in _credit_verticals(chain, expiry, "CE", step, ctx)]
        elif strategy == "BULL_CALL_SPREAD":
            candidates += [(strategy, l, st) for l, st in _debit_verticals(chain, expiry, "CE", step, a.atm_strike, ctx)]
        elif strategy == "BEAR_PUT_SPREAD":
            candidates += [(strategy, l, st) for l, st in _debit_verticals(chain, expiry, "PE", step, a.atm_strike, ctx)]
        elif strategy == "IRON_CONDOR":
            puts = sorted(_credit_verticals(chain, expiry, "PE", step, ctx), key=lambda c: -c[1]["ev_per_risk"])[:6]
            calls = sorted(_credit_verticals(chain, expiry, "CE", step, ctx), key=lambda c: -c[1]["ev_per_risk"])[:6]
            for pl, _ in puts:
                for cl, _ in calls:
                    legs = pl + cl
                    stats = evaluate(legs, ctx)
                    if stats:
                        candidates.append((strategy, legs, stats))

    budget = cfg.risk.capital * cfg.risk.risk_per_trade_pct / 100
    ideas, over_budget = [], 0
    for strategy, legs, stats in candidates:
        if (stats["max_loss"] + stats["costs"]) * a.lot_size > budget:
            over_budget += 1  # cannot trade even one lot within the per-trade risk limit
            continue
        if stats["ev_per_risk"] < s.min_ev_per_risk:
            continue
        protected = _wall_protected(legs, a)
        confidence = _confidence(strategy, view, stats, protected, cfg)
        kind = STRATEGY_KIND[strategy]
        take, stop = _exit_levels(kind, stats, cfg)
        reasons = list(regime["notes"])
        reasons.append(f"Edge after costs: EV {stats['ev']:+.2f}/unit ({stats['ev_per_risk']:+.1%} of max loss), "
                       f"POP {stats['pop']:.0%} using fat-tailed physical vol {phys_vol:.1%}")
        if view.vrp is not None:
            reasons.append(f"ATM IV {a.atm_iv:.1%} vs 20d realised {view.realized_vol:.1%} (VRP {view.vrp:+.1%})")
        if protected:
            reasons.append(f"Short strikes sit beyond OI walls (put wall {a.put_wall:g}, call wall {a.call_wall:g})"
                           if a.put_wall and a.call_wall else "Short strike sits beyond the OI wall")
        reasons.append(f"Direction score {view.direction:+.2f} (trend {view.trend:+.2f}, OI {a.oi_sentiment:+.2f}); "
                       f"dealer gamma {a.gex_regime}")
        ideas.append(TradeIdea(
            id=uuid.uuid4().hex[:10], symbol=a.symbol, strategy=strategy, kind=kind,
            bias=STRATEGY_BIAS[strategy], expiry=a.expiry, legs=legs, lot_size=a.lot_size,
            entry_value=round(stats["entry_value"], 2), max_profit=round(stats["max_profit"], 2),
            max_loss=round(stats["max_loss"], 2), breakevens=[round(b, 1) for b in stats["breakevens"]],
            pop=round(stats["pop"], 4), ev=round(stats["ev"], 2), ev_per_risk=round(stats["ev_per_risk"], 4),
            costs_per_lot=round(stats["costs"] * a.lot_size, 2), confidence=confidence,
            take_profit=take, stop_loss=stop, reasons=reasons, created_at=now.isoformat(),
        ))
    ideas.sort(key=lambda i: (i.confidence, i.ev_per_risk), reverse=True)
    ideas = [i for i in ideas if i.confidence >= s.min_confidence]
    if not ideas and candidates:
        if over_budget == len(candidates):
            regime["notes"].append("Every candidate's one-lot max loss exceeds the per-trade risk budget")
        else:
            regime["notes"].append("Candidates found but none cleared the EV and confidence thresholds")
    elif not candidates:
        regime["notes"].append("No liquid structure met the strike, credit and liquidity filters")
    return regime, ideas[:3]
