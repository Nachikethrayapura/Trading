"""Indian options transaction costs and paper-fill slippage."""
from __future__ import annotations

from .config import CostConfig


def round_tick(price: float, tick: float) -> float:
    return max(round(round(price / tick) * tick, 2), tick)


def order_costs(fills: list[tuple[int, float, int]], cfg: CostConfig) -> float:
    """Charges for a set of option orders. Each fill is (side +1 buy / -1 sell, price, quantity)."""
    buy_turnover = sum(p * q for s, p, q in fills if s > 0)
    sell_turnover = sum(p * q for s, p, q in fills if s < 0)
    turnover = buy_turnover + sell_turnover
    brokerage = cfg.brokerage_per_order * len(fills)
    stt = sell_turnover * cfg.stt_sell_pct / 100
    exchange = turnover * cfg.exchange_txn_pct / 100
    sebi = turnover * cfg.sebi_fee_per_crore / 1e7
    stamp = buy_turnover * cfg.stamp_duty_buy_pct / 100
    gst = (brokerage + exchange + sebi) * cfg.gst_pct / 100
    return brokerage + stt + exchange + sebi + stamp + gst


def round_trip_costs(legs: list[tuple[int, float]], quantity: int, cfg: CostConfig) -> float:
    """Estimated entry + exit charges, assuming the exit happens near entry prices."""
    entry = [(side, price, quantity) for side, price in legs]
    exit_ = [(-side, price, quantity) for side, price in legs]
    return order_costs(entry, cfg) + order_costs(exit_, cfg)


def fill_price(side: int, mid: float, bid: float, ask: float, cfg: CostConfig) -> float:
    """Pessimistic paper fill: buys pay above mid, sells receive below mid."""
    spread = ask - bid if bid > 0 and ask >= bid else 0.0
    slip = max(cfg.tick_size * cfg.slippage_ticks, spread * cfg.slippage_spread_frac)
    return round_tick(mid + side * slip, cfg.tick_size)
