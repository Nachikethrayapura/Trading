from datetime import date, datetime, time

import pytest

from optionsbot.backtest import ENTRY_TICKS, ReplayProvider, day_path, read_csv_bars, run_backtest
from optionsbot.config import load_config
from optionsbot.data.simulated import SimulatedProvider
from optionsbot.models import IST, Bar


@pytest.fixture(scope="module")
def history():
    cfg = load_config()
    cfg.data.provider = "simulated"
    cfg.data.sim_seed = 5
    sim = SimulatedProvider(cfg, warmup_days=160)
    daily = {s: list(sim.states[s].daily) for s in cfg.symbols}
    vix = [Bar(b.ts, v, v, v, v) for b, v in zip(daily["NIFTY"], sim.vix_closes)]
    return daily, vix


def test_day_path_orders_extremes_by_direction():
    up = Bar(datetime(2026, 1, 5, tzinfo=IST), 100, 110, 95, 108)
    down = Bar(datetime(2026, 1, 5, tzinfo=IST), 100, 104, 90, 92)
    assert day_path(up) == [100, 95, 110, 108]
    assert day_path(down) == [100, 104, 90, 92]


def test_replay_has_no_lookahead(history):
    daily, vix = history
    cfg = load_config()
    p = ReplayProvider(cfg, daily, vix)
    for _ in range(12):
        p.advance()
        today = p.daily["NIFTY"][p.day]
        bars = p.daily_bars("NIFTY")
        assert bars[-1].close == pytest.approx(p.option_chain("NIFTY").spot, abs=0.01)  # ends at current tick
        assert bars[-2].ts.date() < today.ts.date()  # nothing from later days
        seen = day_path(today)[: p.tick + 1]
        assert bars[-1].high == max(seen) and bars[-1].low == min(seen)
        assert p.india_vix() == vix[p.day - 1].close  # yesterday's VIX
        assert (p.entry_blocker() is None) == (p.tick in ENTRY_TICKS)
        assert not p.option_chain("NIFTY").oi_available


def test_backtest_runs_and_respects_risk_limits(history):
    daily, vix = history
    cfg = load_config()
    report = run_backtest(cfg, ReplayProvider(cfg, daily, vix))
    assert report["period"]["start"] < report["period"]["end"]
    budget = cfg.risk.capital * cfg.risk.risk_per_trade_pct / 100
    for t in report["trades"]:
        assert t["status"] == "closed"
        assert t["max_loss"] * t["quantity"] <= budget * 1.05  # fills can add a little slippage
        assert datetime.fromisoformat(t["entry_time"]).time() == time(9, 45)
    assert report["equity_curve"]


def test_csv_loader_handles_nse_format(tmp_path):
    f = tmp_path / "NIFTY.csv"
    f.write_text('Date ,Open ,High ,Low ,Close \n"02-Jan-2026","26,000.5","26,100","25,900","26,050"\n')
    bars = read_csv_bars(f)
    assert bars[0].ts.date() == date(2026, 1, 2) and bars[0].open == 26000.5
