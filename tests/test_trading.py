from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from optionsbot.bot import TradingBot
from optionsbot.config import load_config
from optionsbot.costs import order_costs
from optionsbot.data.nse import parse_chain
from optionsbot.data.simulated import SimulatedProvider
from optionsbot.models import IST
from optionsbot.paper import PaperBroker
from optionsbot.risk import RiskManager
from optionsbot.server import create_app
from optionsbot.storage import Store
from optionsbot.strategies import Leg, TradeIdea


@pytest.fixture
def cfg():
    c = load_config()
    c.data.provider = "simulated"
    c.data.sim_seed = 11
    return c


@pytest.fixture
def sim(cfg):
    return SimulatedProvider(cfg, warmup_days=40)


def _bull_put(chain, now, lots=1, short_offset=4, width=2):
    expiry = sorted(chain.expiries)[1]
    step = chain.strike_step(expiry)
    atm = round(chain.spot / step) * step
    short = chain.get(expiry, "PE", atm - short_offset * step)
    long = chain.get(expiry, "PE", atm - (short_offset + width) * step)
    legs = [Leg("PE", short.strike, expiry.isoformat(), -1, short.mid, short.iv, -0.2),
            Leg("PE", long.strike, expiry.isoformat(), +1, long.mid, long.iv, -0.1)]
    credit = short.mid - long.mid
    return TradeIdea(
        id="t1", symbol=chain.symbol, strategy="BULL_PUT_SPREAD", kind="credit", bias="BULLISH",
        expiry=expiry.isoformat(), legs=legs, lot_size=chain.lot_size, entry_value=-credit,
        max_profit=credit, max_loss=width * step - credit, breakevens=[short.strike - credit], pop=0.7,
        ev=1.0, ev_per_risk=0.05, costs_per_lot=100.0, confidence=0.7, take_profit=credit * 0.5,
        stop_loss=-credit, reasons=[], created_at=now.isoformat(), lots=lots)


def test_costs_include_sell_side_stt():
    c = load_config().costs
    buy_only = order_costs([(+1, 100.0, 65)], c)
    sell_only = order_costs([(-1, 100.0, 65)], c)
    assert sell_only - buy_only == pytest.approx(6500 * c.stt_sell_pct / 100 - 6500 * c.stamp_duty_buy_pct / 100)


def test_nse_payload_parsing():
    payload = {"records": {
        "timestamp": "19-Oct-2026 11:30:00", "underlyingValue": 25012.5, "expiryDates": ["27-Oct-2026"],
        "data": [{"strikePrice": 25000, "expiryDate": "27-Oct-2026",
                  "CE": {"lastPrice": 120.5, "bidprice": 120, "askPrice": 121, "impliedVolatility": 12.4,
                         "openInterest": 51234, "changeinOpenInterest": -120, "totalTradedVolume": 900},
                  "PE": {"lastPrice": 98, "buyPrice1": 97.5, "sellPrice1": 98.5, "impliedVolatility": 13.1,
                         "openInterest": 61000, "changeinOpenInterest": 4000, "totalTradedVolume": 1200}}]}}
    chain = parse_chain(payload, "NIFTY", 65)
    assert chain.spot == 25012.5 and chain.timestamp.tzinfo is not None
    ce = chain.get(chain.expiries[0], "CE", 25000)
    pe = chain.get(chain.expiries[0], "PE", 25000)
    assert ce.mid == 120.5 and ce.iv == pytest.approx(0.124)
    assert pe.bid == 97.5 and pe.oi_change == 4000


def test_risk_sizing_respects_budget(cfg):
    rm = RiskManager(cfg)
    idea = TradeIdea(id="x", symbol="NIFTY", strategy="BULL_PUT_SPREAD", kind="credit", bias="BULLISH",
                     expiry="2026-10-27", legs=[], lot_size=65, entry_value=-20, max_profit=20, max_loss=30,
                     breakevens=[], pop=0.7, ev=1, ev_per_risk=0.03, costs_per_lot=100, confidence=0.7,
                     take_profit=10, stop_loss=-20, reasons=[], created_at="")
    budget = cfg.risk.capital * cfg.risk.risk_per_trade_pct / 100
    lots = rm.size(idea)
    assert lots == int(budget // (30 * 65 + 100))
    assert lots * (30 * 65 + 100) <= budget
    idea.max_loss = 1000  # one lot alone blows the budget
    assert rm.size(idea) == 0


def test_paper_take_profit_and_stop(cfg, sim):
    broker = PaperBroker(cfg, Store(":memory:"))
    chain = sim.option_chain("NIFTY")
    now = sim.now()
    pos = broker.open(_bull_put(chain, now), chain, now, lots=2)
    assert pos.quantity == 2 * chain.lot_size and pos.entry_value < 0
    assert pos.unrealized_pnl < 0  # costs and slippage are charged up front

    pos.take_profit = -1e9  # force the profit rule
    closed = broker.update(chain, now)
    assert closed and closed[0].exit_reason.startswith("Take profit")
    assert closed[0].realized_pnl == pytest.approx(
        (closed[0].exit_value - closed[0].entry_value) * closed[0].quantity
        - closed[0].entry_costs - closed[0].exit_costs, abs=0.01)

    pos = broker.open(_bull_put(chain, now), chain, now, lots=1)
    pos.stop_loss = 1e9
    pos.take_profit = 2e9
    assert broker.update(chain, now)[0].exit_reason.startswith("Stop loss")


def test_expired_position_settles_at_intrinsic(cfg, sim):
    broker = PaperBroker(cfg, Store(":memory:"))
    chain = sim.option_chain("NIFTY")
    now = sim.now()
    pos = broker.open(_bull_put(chain, now), chain, now, lots=1)
    later = datetime.fromisoformat(pos.expiry).replace(hour=16, tzinfo=IST)
    closed = broker.update(chain, later)
    assert closed[0].exit_reason.startswith("Expired") and closed[0].exit_costs == 0


def test_circuit_breaker_halts_after_daily_loss(cfg, sim):
    store = Store(":memory:")
    broker = PaperBroker(cfg, store)
    rm = RiskManager(cfg)
    chain = sim.option_chain("NIFTY")
    now = sim.now()
    pos = broker.open(_bull_put(chain, now), chain, now, lots=1)
    pos.unrealized_pnl = -cfg.risk.capital  # simulate a catastrophic mark
    halted, flatten = rm.check_circuit_breaker(broker, now)
    assert halted and flatten
    assert any("Circuit breaker" in b for b in rm.trade_blockers(_bull_put(chain, now), 1, broker, now))
    # The halt lifts on the next trading day.
    broker.positions.clear()
    assert rm.check_circuit_breaker(broker, now + timedelta(days=1)) == (False, False)


def test_entry_window_and_shock_blockers(cfg):
    rm = RiskManager(cfg)
    early = datetime(2026, 10, 19, 9, 20, tzinfo=IST)
    assert any("entry window" in b for b in rm.entry_blockers("NIFTY", early, False, False))
    t = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
    rm.record_spot("NIFTY", t, 25000)
    rm.record_spot("NIFTY", t + timedelta(minutes=5), 25500)
    assert any("shock" in b for b in rm.entry_blockers("NIFTY", t + timedelta(minutes=5), False, False))
    assert any("stale" in b for b in rm.entry_blockers("NIFTY", t, True, False))


def test_bot_runs_on_simulator_and_only_takes_defined_risk(cfg, sim):
    bot = TradingBot(cfg, provider=sim, store=Store(":memory:"))
    budget = cfg.risk.capital * cfg.risk.risk_per_trade_pct / 100
    for _ in range(300):
        state = bot.step()
        assert not state["errors"]
        for d in state["symbols"].values():
            for idea in d["ideas"]:
                assert idea["max_loss"] > 0 and idea["max_loss_per_lot"] <= budget
                assert sum(l["side"] for l in idea["legs"]) == 0  # every short leg is covered
        assert len(state["positions"]) <= cfg.risk.max_open_positions
        assert state["risk"]["open_risk"] <= state["risk"]["open_risk_limit"] + 1e-6
    assert state["equity_curve"]


def test_api_endpoints(cfg, sim):
    bot = TradingBot(cfg, provider=sim, store=Store(":memory:"))
    bot.step()
    with TestClient(create_app(bot, run_loop=False)) as client:
        assert client.get("/").status_code == 200
        state = client.get("/api/state").json()
        assert set(state["symbols"]) == {"NIFTY", "BANKNIFTY"}
        assert client.post("/api/bot/pause").json() == {"paused": True}
        assert client.post("/api/ideas/missing/paper").status_code == 404
        assert client.post("/api/positions/missing/close").status_code == 404
        assert client.post("/api/flatten").json() == {"ok": True}
        with client.websocket_connect("/ws") as ws:
            assert "symbols" in ws.receive_json()
