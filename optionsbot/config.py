"""Configuration: typed defaults, overridable from a YAML file."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DataConfig:
    provider: str = "nse"  # nse (public website) | upstox | kite (real-time broker APIs) | simulated
    poll_seconds: float = 60.0  # NSE rate-limits aggressively; keep >= 30s for live data
    stale_after_seconds: int = 300  # chain older than this blocks new entries
    request_timeout: float = 10.0
    max_expiries: int = 3  # expiries fetched per symbol (v3 API needs one request each)
    strikes_each_side: int = 25  # strikes around spot requested from broker APIs
    broker_oi_unit: str = "shares"  # "shares" or "contracts": how the broker reports OI (verify vs NSE site)
    instrument_keys: dict[str, str] = field(default_factory=dict)  # Upstox keys for stocks, e.g. NSE_EQ|INE002A01018
    sim_minutes_per_tick: int = 5
    sim_seed: int | None = None


@dataclass
class RiskConfig:
    capital: float = 500_000.0
    risk_per_trade_pct: float = 1.0  # max loss of a single trade as % of capital
    max_portfolio_risk_pct: float = 5.0  # sum of max losses across open trades
    max_daily_loss_pct: float = 2.0  # realized + unrealized; trips the circuit breaker
    max_open_positions: int = 3
    max_lots_per_trade: int = 10
    max_consecutive_losses: int = 3  # halts new entries for the rest of the day
    flatten_on_daily_loss: bool = True
    one_position_per_symbol: bool = True


@dataclass
class ExitConfig:
    credit_take_profit_pct: float = 50.0  # close credit trades at 50% of max profit
    credit_stop_loss_multiple: float = 1.0  # close when loss reaches 1x the credit received
    debit_take_profit_pct: float = 60.0
    debit_stop_loss_pct: float = 50.0  # close when half the debit is lost
    short_delta_exit: float = 0.45  # short strike being tested -> exit
    expiry_day_exit_time: str = "14:45"


@dataclass
class StrategyConfig:
    min_dte: int = 2  # avoid expiry-day gamma
    max_dte: int = 40  # monthly-only indices can sit 36 days from the next expiry
    short_delta_min: float = 0.12
    short_delta_max: float = 0.30
    max_wing_steps: int = 4
    min_credit_to_width: float = 0.15
    max_debit_to_width: float = 0.60
    min_confidence: float = 0.55
    min_ev_per_risk: float = 0.03  # expected value / max loss, after costs
    min_oi: float = 500  # contracts
    max_bid_ask_pct: float = 8.0
    iv_rich_rank: float = 40.0
    iv_cheap_rank: float = 25.0
    trend_threshold: float = 0.30
    strong_trend_threshold: float = 0.50
    trend_edge: float = 0.25  # drift assumed at max trend score, in units of 1-sigma move
    vrp_weight: float = 0.5  # physical vol = w*realized + (1-w)*implied
    tail_prob: float = 0.10  # weight of the fat-tail regime in the payoff distribution
    tail_vol_mult: float = 2.0
    entry_start: str = "09:30"
    entry_end: str = "14:30"
    signal_cooldown_minutes: int = 30
    shock_move_pct: float = 1.2
    shock_window_minutes: int = 15
    event_days: list[str] = field(default_factory=list)  # YYYY-MM-DD: no new entries
    enabled_strategies: list[str] = field(default_factory=lambda: [
        "IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"])


@dataclass
class CostConfig:
    # Indian F&O charges. Verify against your broker's current schedule.
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.15  # on option premium, sell side
    exchange_txn_pct: float = 0.03503
    sebi_fee_per_crore: float = 10.0
    stamp_duty_buy_pct: float = 0.003
    gst_pct: float = 18.0
    slippage_ticks: int = 1
    slippage_spread_frac: float = 0.25  # paper fills cross this fraction of the bid-ask spread
    tick_size: float = 0.05


@dataclass
class AlertsConfig:
    enabled: bool = True  # alerts only go out when a bot token and chat id are set
    telegram_bot_token: str = ""  # or env TELEGRAM_BOT_TOKEN
    telegram_chat_id: str = ""  # or env TELEGRAM_CHAT_ID
    recommendations: bool = True  # alert on new actionable ideas when auto paper-trading is off
    recommendation_cooldown_minutes: int = 30
    data_error_cooldown_minutes: int = 60


@dataclass
class Config:
    symbols: list[str] = field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])
    # NSE revises lot sizes periodically; keep these in sync with the latest circular.
    lot_sizes: dict[str, int] = field(
        default_factory=lambda: {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120}
    )
    risk_free_rate: float = 0.06
    auto_paper_trade: bool = True
    db_path: str = "data/paper_trades.db"
    host: str = "127.0.0.1"
    port: int = 8000
    data: DataConfig = field(default_factory=DataConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)


def _merge(cls: type, values: dict[str, Any], path: str = "") -> Any:
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(values) - set(known)
    if unknown:
        raise ValueError(f"Unknown config keys at '{path or 'root'}': {sorted(unknown)}")
    kwargs = {}
    for name, value in values.items():
        default = getattr(cls(), name)
        if dataclasses.is_dataclass(default) and isinstance(value, dict):
            kwargs[name] = _merge(type(default), value, f"{path}{name}.")
        elif isinstance(default, dict) and isinstance(value, dict):
            kwargs[name] = {**default, **value}
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None = None, **overrides: Any) -> Config:
    values: dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            values = yaml.safe_load(fh) or {}
    cfg = _merge(Config, values)
    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)
    cfg.symbols = [s.upper() for s in cfg.symbols]
    return cfg
