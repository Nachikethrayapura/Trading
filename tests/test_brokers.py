import hashlib
from datetime import timedelta

import pytest

from optionsbot.analytics.chain_metrics import analyze_chain
from optionsbot.auth import _param_from, kite_checksum
from optionsbot.config import load_config
from optionsbot.data import brokers
from optionsbot.data.base import DataUnavailable
from optionsbot.models import now_ist


class FakeResponse:
    def __init__(self, payload=None, text="", status=200):
        self._payload, self.text, self.status_code = payload, text, status

    def json(self):
        return self._payload


@pytest.fixture
def expiry():
    d = now_ist().date() + timedelta(days=7)
    return d


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(brokers, "TOKEN_FILE", tmp_path / "tokens.json")
    return load_config()


def _route(monkeypatch, handler):
    calls = []

    def get(self, url, params=None, timeout=None, **kw):
        calls.append((url, params, dict(self.headers)))
        return handler(url, params)

    monkeypatch.setattr(brokers.requests.Session, "get", get)
    return calls


def test_upstox_chain_parsing(monkeypatch, cfg, expiry):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "tok")

    def leg(ltp, iv, oi, prev):
        return {"market_data": {"ltp": ltp, "bid_price": ltp - 0.5, "ask_price": ltp + 0.5, "oi": oi,
                                "prev_oi": prev, "volume": 1000},
                "option_greeks": {"iv": iv, "delta": 0.5}}

    def handler(url, params):
        if url.endswith("/option/contract"):
            return FakeResponse({"status": "success", "data": [
                {"expiry": expiry.isoformat(), "lot_size": 65, "strike_price": k, "instrument_type": t}
                for k in (25000, 25050) for t in ("CE", "PE")]})
        if url.endswith("/option/chain"):
            assert params["expiry_date"] == expiry.isoformat()
            return FakeResponse({"status": "success", "data": [
                {"strike_price": k, "underlying_spot_price": 25020.5,
                 "call_options": leg(120.0, 13.5, 650000, 585000), "put_options": leg(95.0, 14.2, 1300000, 1300000)}
                for k in (25000, 25050)]})
        if url.endswith("/market-quote/ltp"):
            return FakeResponse({"status": "success", "data": {"NSE_INDEX:India VIX": {"last_price": 11.8}}})
        raise AssertionError(url)

    calls = _route(monkeypatch, handler)
    p = brokers.UpstoxProvider(cfg)
    chain = p.option_chain("NIFTY")
    assert chain.spot == 25020.5 and chain.lot_size == 65 and chain.expiries == [expiry]
    ce = chain.get(expiry, "CE", 25000)
    assert ce.iv == pytest.approx(0.135) and ce.bid == 119.5 and ce.ask == 120.5
    assert ce.oi == 10000 and ce.oi_change == 1000  # shares -> contracts (lot 65)
    assert p.india_vix() == 11.8
    assert calls[0][2]["Authorization"] == "Bearer tok"
    assert calls[0][1]["instrument_key"] == "NSE_INDEX|Nifty 50"
    analyze_chain(chain, expiry, now_ist(), cfg.risk_free_rate)  # analytics accept the broker chain


def test_kite_chain_parsing(monkeypatch, cfg, expiry):
    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "tok")
    rows = ["instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
            "instrument_type,segment,exchange"]
    for k in (24950, 25000, 25050, 30000):
        for t in ("CE", "PE"):
            rows.append(f"1,1,NIFTYX{k}{t},NIFTY,0,{expiry.isoformat()},{k},0.05,65,{t},NFO-OPT,NFO")
    rows.append(f"2,2,BANKNIFTYX55000CE,BANKNIFTY,0,{expiry.isoformat()},55000,0.05,30,CE,NFO-OPT,NFO")
    oi = {"value": 650000}

    def handler(url, params):
        if url.endswith("/instruments/NFO"):
            return FakeResponse(text="\n".join(rows))
        if url.endswith("/quote/ltp"):
            return FakeResponse({"status": "success", "data": {params["i"]: {"last_price": 25010.0}}})
        if url.endswith("/quote"):
            keys = [v for _, v in params]
            assert all("BANKNIFTY" not in k for k in keys)
            return FakeResponse({"status": "success", "data": {k: {
                "last_price": 100.0, "volume": 10, "oi": oi["value"],
                "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100.5}]}} for k in keys}})
        raise AssertionError(url)

    calls = _route(monkeypatch, handler)
    cfg.data.strikes_each_side = 1
    p = brokers.KiteProvider(cfg)
    chain = p.option_chain("NIFTY")
    assert chain.spot == 25010.0 and chain.lot_size == 65
    assert sorted({q.strike for q in chain.quotes}) == [24950, 25000, 25050]  # 30000 is outside the window
    q = chain.get(expiry, "PE", 25000)
    assert (q.bid, q.ask, q.oi, q.oi_change, q.iv) == (99.5, 100.5, 10000, 0, 0.0)
    oi["value"] = 715000
    assert p.option_chain("NIFTY").get(expiry, "PE", 25000).oi_change == 1000  # vs first reading today
    assert calls[0][2]["Authorization"] == "token key:tok"


def test_broker_errors_are_reported(monkeypatch, cfg):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    with pytest.raises(DataUnavailable, match="auth upstox"):
        brokers.UpstoxProvider(cfg)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "expired")
    _route(monkeypatch, lambda url, params: FakeResponse({"status": "error"}, status=401))
    with pytest.raises(DataUnavailable, match="token rejected"):
        brokers.UpstoxProvider(cfg).option_chain("NIFTY")


def test_token_file_round_trip(monkeypatch, cfg):
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    brokers.save_token("kite", "abc")
    assert brokers.load_token("kite", "KITE_ACCESS_TOKEN") == "abc"
    assert oct(brokers.TOKEN_FILE.stat().st_mode & 0o777) == "0o600"


def test_auth_helpers():
    assert _param_from("https://127.0.0.1/cb?request_token=xyz&action=login", "request_token") == "xyz"
    assert _param_from("plain-code", "code") == "plain-code"
    assert kite_checksum("k", "r", "s") == hashlib.sha256(b"krs").hexdigest()


def test_nse_market_status_detects_holiday(monkeypatch):
    from optionsbot.data import nse
    cfg = load_config()
    p = nse.NSEProvider(cfg)
    monkeypatch.setattr(p.nse, "get_json", lambda path, params=None: {"marketState": [
        {"market": "Capital Market", "marketStatus": "Close"}, {"market": "Commodity", "marketStatus": "Open"}]})
    assert p.market_open() is False

    def down(path, params=None):
        raise DataUnavailable("blocked")
    p._status = None
    monkeypatch.setattr(p.nse, "get_json", down)
    from optionsbot.models import is_market_open, now_ist
    assert p.market_open() == is_market_open(now_ist())  # falls back to the clock
