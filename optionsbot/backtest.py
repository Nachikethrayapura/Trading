"""Historical backtest: replays daily index prices through the live engine, risk manager and paper broker.

Free historical NSE option-chain data does not exist, so each day's chain is reconstructed from
the index price and the *previous* day's India VIX close (VIX is the market's 30-day implied vol
for NIFTY, scaled for other indices), using the same smile and spread model as the simulator.
OI-based signals (GEX, walls, PCR) are unavailable and treated as neutral.

Each day is replayed as four ticks: 09:45 open, the two extremes, 14:50 close. The extremes
follow the day's direction (low first on up days), so stops and targets are hit along a plausible
intraday path. New entries only happen on the open tick: any later tick would leak information
about the rest of the day (entering at extremes biases results down, at a midpoint biases them up). Treat results as a sanity check of the rules and
risk controls, not a forecast: real option prices, skew and liquidity differ from the model.

Usage:
    python -m optionsbot.backtest --symbols NIFTY BANKNIFTY --years 3
    python -m optionsbot.backtest --csv-dir history/   # NIFTY.csv, INDIAVIX.csv, ... (date,open,high,low,close)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path

from .bot import TradingBot, performance
from .config import Config, load_config
from .data.base import DataUnavailable, MarketDataProvider
from .data.simulated import sim_expiries
from .data.synthetic import build_chain
from .data.yahoo import YahooClient
from .models import IST, Bar, OptionChain
from .storage import Store

# India VIX tracks NIFTY 30-day IV; other underlyings typically trade at a premium to it.
IV_SCALE = {"NIFTY": 1.0, "BANKNIFTY": 1.2, "FINNIFTY": 1.1, "MIDCPNIFTY": 1.3}
STOCK_IV_SCALE = 1.6
STRIKE_STEP = {"NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0, "MIDCPNIFTY": 25.0}
TICK_TIMES = (time(9, 45), time(11, 30), time(13, 15), time(14, 50))
ENTRY_TICKS = {0}  # only the open is known without peeking at the rest of the day
WARMUP_DAYS = 260
MIN_WARMUP_DAYS = 60
VIX = "INDIAVIX"


def _stock_step(price: float) -> float:
    for limit, step in ((250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50)):
        if price < limit:
            return step
    return 100.0


def day_path(bar: Bar) -> list[float]:
    """Open, the two extremes (low first on up days), close."""
    first, second = (bar.low, bar.high) if bar.close >= bar.open else (bar.high, bar.low)
    return [bar.open, first, second, bar.close]


class ReplayProvider(MarketDataProvider):
    name = "Historical replay (VIX-reconstructed chains)"
    simulated = True  # runs on its own clock

    def __init__(self, cfg: Config, daily: dict[str, list[Bar]], vix: list[Bar],
                 start: date | None = None, end: date | None = None,
                 iv_scale: dict[str, float] | None = None) -> None:
        self.cfg = cfg
        self.iv_scale = {**IV_SCALE, **(iv_scale or {})}
        by_day = {s: {b.ts.date(): b for b in bars} for s, bars in daily.items()}
        vix_by_day = {b.ts.date(): b for b in vix}
        days = sorted(set(vix_by_day).intersection(*[set(d) for d in by_day.values()]))
        if len(days) < MIN_WARMUP_DAYS + 5:
            raise DataUnavailable(f"Need at least {MIN_WARMUP_DAYS + 5} overlapping days of history, got {len(days)}")
        self.days = days
        self.daily = {s: [by_day[s][d] for d in days] for s in daily}
        self.vix = [vix_by_day[d] for d in days]
        warmup = min(WARMUP_DAYS, len(days) // 3) if len(days) < 2 * WARMUP_DAYS else WARMUP_DAYS
        first = max(warmup, MIN_WARMUP_DAYS)
        if start:
            first = max(first, next((i for i, d in enumerate(days) if d >= start), len(days)))
        self.first_day = first
        self.last_day = max((i for i, d in enumerate(days) if not end or d <= end), default=-1)
        self.day = first
        self.tick = -1
        self.intraday: dict[str, list[Bar]] = {s: [] for s in daily}
        self._seed_intraday()

    def _seed_intraday(self) -> None:
        for symbol, bars in self.daily.items():
            for k in range(max(0, self.day - 10), self.day):
                path = day_path(bars[k])
                for i, price in enumerate(path):
                    prev = path[i - 1] if i else price
                    ts = datetime.combine(self.days[k], TICK_TIMES[i], tzinfo=IST)
                    self.intraday[symbol].append(Bar(ts, prev, max(prev, price), min(prev, price), price))

    # ----------------------------------------------------------------- clock
    @property
    def done(self) -> bool:
        return self.day > self.last_day or (self.day == self.last_day and self.tick == len(TICK_TIMES) - 1)

    def advance(self) -> None:
        if self.done:
            raise StopIteration
        self.tick += 1
        if self.tick == len(TICK_TIMES):
            self.day, self.tick = self.day + 1, 0
        for symbol in self.daily:
            price, prev = self._price(symbol), self._prev_price(symbol)
            self.intraday[symbol].append(Bar(self.now(), prev, max(prev, price), min(prev, price), price))
            del self.intraday[symbol][:-40]

    def now(self) -> datetime:
        return datetime.combine(self.days[self.day], TICK_TIMES[max(self.tick, 0)], tzinfo=IST)

    def _price(self, symbol: str) -> float:
        return day_path(self.daily[symbol][self.day])[max(self.tick, 0)]

    def _prev_price(self, symbol: str) -> float:
        if self.tick > 0:
            return day_path(self.daily[symbol][self.day])[self.tick - 1]
        return self.daily[symbol][self.day - 1].close

    def entry_blocker(self) -> str | None:
        return None if self.tick in ENTRY_TICKS else "Backtest: entries only at the open tick"

    # ----------------------------------------------------------------- data API
    def option_chain(self, symbol: str) -> OptionChain:
        spot = self._price(symbol)
        vix = self.vix[self.day - 1].close  # yesterday's close: no lookahead
        scale = self.iv_scale.get(symbol, STOCK_IV_SCALE)
        step = STRIKE_STEP.get(symbol) or _stock_step(spot)
        lot = self.cfg.lot_sizes.get(symbol)
        if not lot:
            raise DataUnavailable(f"Lot size for {symbol} missing from config")
        now = self.now()
        return build_chain(symbol, spot, vix / 100 * scale, now, sim_expiries(symbol, now), step,
                           self.cfg.risk_free_rate, lot)

    def intraday_bars(self, symbol: str) -> list[Bar]:
        return list(self.intraday[symbol])

    def daily_bars(self, symbol: str) -> list[Bar]:
        bars = self.daily[symbol][max(0, self.day - 300):self.day]
        today = day_path(self.daily[symbol][self.day])[: max(self.tick, 0) + 1]
        partial = Bar(self.now(), today[0], max(today), min(today), today[-1])
        return bars + [partial]

    def india_vix(self) -> float | None:
        return self.vix[self.day - 1].close

    def vix_history(self) -> list[float]:
        return [b.close for b in self.vix[max(0, self.day - 252):self.day]]


# ----------------------------------------------------------------- loading
def load_yahoo(symbols: list[str], years: int) -> tuple[dict[str, list[Bar]], list[Bar]]:
    client = YahooClient(timeout=20)
    span = f"{years + 2}y"  # extra history for the warm-up window
    return {s: client.bars(s, "1d", span) for s in symbols}, client.bars(VIX, "1d", span)


def _parse_csv_date(value: str) -> date:
    value = value.strip().strip('"')
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d %b %Y", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return datetime.fromisoformat(value).date()


def read_csv_bars(path: Path) -> list[Bar]:
    """CSV with date, open, high, low, close columns (any case; NSE and Yahoo exports both work)."""
    bars = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {name.strip().lower(): name for name in reader.fieldnames or []}
        need = {"date", "open", "high", "low", "close"}
        if not need <= set(cols):
            raise DataUnavailable(f"{path} needs columns {sorted(need)}, found {sorted(cols)}")
        for row in reader:
            try:
                o, h, l, c = (float(str(row[cols[k]]).replace(",", "")) for k in ("open", "high", "low", "close"))
            except ValueError:
                continue
            d = _parse_csv_date(row[cols["date"]])
            bars.append(Bar(datetime.combine(d, time(9, 15), tzinfo=IST), o, h, l, c))
    return sorted(bars, key=lambda b: b.ts)


def load_csv_dir(directory: Path, symbols: list[str]) -> tuple[dict[str, list[Bar]], list[Bar]]:
    return {s: read_csv_bars(directory / f"{s}.csv") for s in symbols}, read_csv_bars(directory / f"{VIX}.csv")


# ----------------------------------------------------------------- running
def run_backtest(cfg: Config, provider: ReplayProvider) -> dict:
    cfg.auto_paper_trade = True
    bot = TradingBot(cfg, provider=provider, store=Store(":memory:"), alerts=False)
    errors: dict[str, int] = defaultdict(int)
    while not provider.done:
        state = bot.step()
        for err in state["errors"]:
            errors[err.split(":")[0] + ": " + err.split(":", 1)[-1].strip()[:80]] += 1
    bot.flatten()  # close anything still open at the last tick
    closed = list(reversed(bot.broker.closed_positions(limit=100000)))
    curve = bot.store.equity_curve(limit=10**7)
    return build_report(cfg, provider, closed, curve, dict(errors))


def build_report(cfg: Config, provider: ReplayProvider, closed: list[dict], curve: list[dict],
                 errors: dict[str, int]) -> dict:
    stats = performance(closed, curve)
    capital = cfg.risk.capital
    peak, max_dd_pct = capital, 0.0
    for p in curve:
        peak = max(peak, p["equity"])
        max_dd_pct = max(max_dd_pct, (peak - p["equity"]) / peak * 100)
    groups: dict[str, dict] = {}
    for key in ("strategy", "symbol"):
        out: dict[str, dict] = {}
        for p in closed:
            g = out.setdefault(p[key], {"trades": 0, "wins": 0, "pnl": 0.0})
            g["trades"] += 1
            g["wins"] += (p["realized_pnl"] or 0) > 0
            g["pnl"] += p["realized_pnl"] or 0
        groups[key] = out
    reasons: dict[str, int] = defaultdict(int)
    for p in closed:
        reasons[(p["exit_reason"] or "").split(":")[0]] += 1
    start, end = provider.days[provider.first_day], provider.days[min(provider.last_day, len(provider.days) - 1)]
    years = max((end - start).days / 365.25, 1e-9)
    final = curve[-1]["equity"] if curve else capital
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "years": round(years, 2)},
        "symbols": list(provider.daily), "capital": capital, "final_equity": round(final, 2),
        "return_pct": round((final / capital - 1) * 100, 2),
        "cagr_pct": round(((final / capital) ** (1 / years) - 1) * 100, 2) if final > 0 else None,
        "max_drawdown_pct": round(max_dd_pct, 2), "stats": stats,
        "by_strategy": groups["strategy"], "by_symbol": groups["symbol"], "exit_reasons": dict(reasons),
        "errors": errors, "trades": closed, "equity_curve": curve,
    }


def write_report(report: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {k: v for k, v in report.items() if k not in ("trades", "equity_curve")}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    with open(out_dir / "trades.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["entry_time", "exit_time", "symbol", "strategy", "legs", "lots", "entry_value",
                    "exit_value", "costs", "realized_pnl", "exit_reason"])
        for p in report["trades"]:
            legs = " / ".join(f"{'B' if l['side'] > 0 else 'S'} {l['strike']:g}{l['option_type']}" for l in p["legs"])
            w.writerow([p["entry_time"], p["exit_time"], p["symbol"], p["strategy"], legs, p["lots"],
                        p["entry_value"], p["exit_value"], round(p["entry_costs"] + p["exit_costs"], 2),
                        p["realized_pnl"], p["exit_reason"]])
    with open(out_dir / "equity.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "equity"])
        w.writerows([p["ts"], round(p["equity"], 2)] for p in report["equity_curve"])


def print_report(report: dict) -> None:
    s = report["stats"]
    pf = s["profit_factor"]
    print(f"\nBacktest {report['period']['start']} -> {report['period']['end']} "
          f"({report['period']['years']} yrs) on {', '.join(report['symbols'])}")
    print(f"  Capital {report['capital']:,.0f} -> {report['final_equity']:,.0f} INR "
          f"({report['return_pct']:+.2f}%, CAGR {report['cagr_pct']}%), max drawdown {report['max_drawdown_pct']:.2f}%")
    if s["trades"]:
        print(f"  Trades {s['trades']}  win rate {s['win_rate']:.0%}  profit factor "
              f"{'n/a' if pf is None else f'{pf:.2f}'}  expectancy {s['expectancy']:+,.0f} INR/trade")
        print(f"  Avg win {s['avg_win'] or 0:+,.0f}  avg loss {s['avg_loss'] or 0:+,.0f}")
    else:
        print("  No trades: the rules found no qualifying edge in this period")
    for title, key in (("By strategy", "by_strategy"), ("By symbol", "by_symbol")):
        if report[key]:
            print(f"  {title}:")
            for name, g in sorted(report[key].items(), key=lambda kv: -kv[1]["pnl"]):
                print(f"    {name:<18} {g['trades']:>4} trades  {g['wins'] / g['trades']:>4.0%} win  {g['pnl']:>+12,.0f} INR")
    if report["exit_reasons"]:
        print("  Exits: " + ", ".join(f"{k} {v}" for k, v in sorted(report["exit_reasons"].items(), key=lambda kv: -kv[1])))
    if report["errors"]:
        print("  Data issues: " + "; ".join(f"{k} (x{v})" for k, v in list(report["errors"].items())[:5]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the options bot on historical index + India VIX data")
    parser.add_argument("--config", help="YAML config (risk, exits, strategy settings are used as-is)")
    parser.add_argument("--symbols", nargs="+", help="default: symbols from the config")
    parser.add_argument("--years", type=int, default=3, help="years to replay when downloading from Yahoo")
    parser.add_argument("--csv-dir", type=Path, help="offline history: <SYMBOL>.csv and INDIAVIX.csv")
    parser.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--out", type=Path, default=Path("reports/backtest"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    cfg = load_config(args.config)
    symbols = [s.upper() for s in (args.symbols or cfg.symbols)]
    cfg.symbols = symbols
    if args.csv_dir:
        daily, vix = load_csv_dir(args.csv_dir, symbols)
    else:
        print(f"Downloading {args.years}y of daily history for {', '.join(symbols)} and India VIX from Yahoo…")
        daily, vix = load_yahoo(symbols, args.years)
    start = args.start
    if start is None and not args.csv_dir:
        start = date.today().replace(year=date.today().year - args.years)
    provider = ReplayProvider(cfg, daily, vix, start=start, end=args.end)
    report = run_backtest(cfg, provider)
    print_report(report)
    write_report(report, args.out)
    print(f"\n  Trades, equity curve and summary written to {args.out}/")


if __name__ == "__main__":
    main()
