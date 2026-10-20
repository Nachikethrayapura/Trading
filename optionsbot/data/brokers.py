"""Real-time option chains from broker APIs: Upstox (v2) and Zerodha Kite Connect (v3).

Both need a daily access token (they expire every morning). Generate one with
`python -m optionsbot.auth upstox` or `python -m optionsbot.auth kite`, or set the env vars
UPSTOX_ACCESS_TOKEN / KITE_API_KEY + KITE_ACCESS_TOKEN yourself.

Price history for the trend signals still comes from Yahoo; the chain, spot and India VIX
come from the broker. Lot sizes come from the broker's contract master, so they stay current.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time as _time
from datetime import date, datetime
from pathlib import Path

import requests

from ..config import Config
from ..models import Bar, OptionChain, OptionQuote, now_ist
from .base import DataUnavailable, MarketDataProvider
from .yahoo import YahooClient

log = logging.getLogger(__name__)
TOKEN_FILE = Path("data/broker_tokens.json")
CONTRACTS_TTL = 6 * 3600.0


def load_token(broker: str, env_var: str) -> str:
    """Access token from the environment, else from the file written by `python -m optionsbot.auth`."""
    token = os.environ.get(env_var, "")
    if token:
        return token
    try:
        saved = json.loads(TOKEN_FILE.read_text()).get(broker, {})
    except (OSError, ValueError):
        saved = {}
    if saved.get("date") and saved["date"] != now_ist().date().isoformat():
        log.warning("Saved %s token is from %s and has probably expired; re-run `python -m optionsbot.auth %s`",
                    broker, saved["date"], broker)
    return saved.get("access_token", "")


def save_token(broker: str, access_token: str, **extra: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    data[broker] = {"access_token": access_token, "date": now_ist().date().isoformat(), **extra}
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    TOKEN_FILE.chmod(0o600)


def _iv_decimal(value: float | None) -> float:
    """Brokers report IV in percent (14.2); NSE-style decimals (0.142) pass through."""
    if not value or value <= 0:
        return 0.0
    return value / 100 if value > 2.0 else value


class _BrokerProvider(MarketDataProvider):
    """Shared plumbing: Yahoo history, contract caching, OI unit conversion."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.yahoo = YahooClient(cfg.data.request_timeout)
        self.session = requests.Session()
        self._contracts: dict[str, tuple[float, object]] = {}

    def _oi(self, raw: float, lot: int) -> float:
        # Brokers usually report OI in shares; the analytics expect contracts (lots), like NSE.
        return raw / lot if self.cfg.data.broker_oi_unit == "shares" and lot else raw

    def _cached_contracts(self, symbol: str, fetch):
        hit = self._contracts.get(symbol)
        if hit and _time.monotonic() - hit[0] < CONTRACTS_TTL:
            return hit[1]
        value = fetch()
        self._contracts[symbol] = (_time.monotonic(), value)
        return value

    def _get(self, url: str, **kwargs) -> requests.Response:
        try:
            resp = self.session.get(url, timeout=self.cfg.data.request_timeout, **kwargs)
        except requests.RequestException as exc:
            raise DataUnavailable(f"{self.name}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise DataUnavailable(f"{self.name}: access token rejected ({resp.status_code}). "
                                  f"Generate a fresh one with `python -m optionsbot.auth`")
        if resp.status_code == 429:
            raise DataUnavailable(f"{self.name}: rate limited; increase data.poll_seconds")
        if resp.status_code != 200:
            raise DataUnavailable(f"{self.name}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp

    def intraday_bars(self, symbol: str) -> list[Bar]:
        return self.yahoo.bars(symbol, "5m", "5d")

    def daily_bars(self, symbol: str) -> list[Bar]:
        return self.yahoo.bars(symbol, "1d", "1y")

    def vix_history(self) -> list[float]:
        return [b.close for b in self.yahoo.bars("INDIAVIX", "1d", "1y")]


# --------------------------------------------------------------------------- Upstox
UPSTOX_BASE = "https://api.upstox.com/v2"
UPSTOX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "INDIAVIX": "NSE_INDEX|India VIX",
}


class UpstoxProvider(_BrokerProvider):
    name = "Upstox (real-time)"

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        token = load_token("upstox", "UPSTOX_ACCESS_TOKEN")
        if not token:
            raise DataUnavailable("Upstox access token missing: run `python -m optionsbot.auth upstox`")
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self.keys = {**UPSTOX_KEYS, **cfg.data.instrument_keys}

    def _key(self, symbol: str) -> str:
        if symbol not in self.keys:
            raise DataUnavailable(f"No Upstox instrument key for {symbol}: add it under data.instrument_keys "
                                  f"(stocks look like 'NSE_EQ|<ISIN>')")
        return self.keys[symbol]

    def _data(self, path: str, params: dict) -> object:
        body = self._get(f"{UPSTOX_BASE}{path}", params=params).json()
        if body.get("status") != "success":
            raise DataUnavailable(f"Upstox {path}: {body.get('errors') or body}")
        return body.get("data")

    def _contracts_for(self, symbol: str) -> tuple[list[date], int]:
        def fetch():
            rows = self._data("/option/contract", {"instrument_key": self._key(symbol)}) or []
            today = now_ist().date()
            expiries = sorted({date.fromisoformat(r["expiry"][:10]) for r in rows})
            lot = next((int(r["lot_size"]) for r in rows if r.get("lot_size")), 0)
            return [e for e in expiries if e >= today], lot
        return self._cached_contracts(symbol, fetch)

    def option_chain(self, symbol: str) -> OptionChain:
        expiries, lot = self._contracts_for(symbol)
        lot = lot or self.cfg.lot_sizes.get(symbol, 0)
        if not expiries or not lot:
            raise DataUnavailable(f"Upstox returned no option contracts for {symbol}")
        quotes, spot = [], 0.0
        for expiry in expiries[: self.cfg.data.max_expiries]:
            rows = self._data("/option/chain", {"instrument_key": self._key(symbol),
                                                "expiry_date": expiry.isoformat()}) or []
            for row in rows:
                spot = spot or float(row.get("underlying_spot_price") or 0)
                strike = float(row["strike_price"])
                for option_type, side in (("CE", "call_options"), ("PE", "put_options")):
                    leg = row.get(side) or {}
                    md, greeks = leg.get("market_data") or {}, leg.get("option_greeks") or {}
                    if not md:
                        continue
                    oi = self._oi(float(md.get("oi") or 0), lot)
                    prev_oi = self._oi(float(md.get("prev_oi") or 0), lot)
                    quotes.append(OptionQuote(
                        strike=strike, option_type=option_type, expiry=expiry,
                        ltp=float(md.get("ltp") or 0), bid=float(md.get("bid_price") or 0),
                        ask=float(md.get("ask_price") or 0), iv=_iv_decimal(greeks.get("iv")),
                        oi=oi, oi_change=oi - prev_oi if prev_oi else 0.0, volume=float(md.get("volume") or 0),
                    ))
        if not quotes or spot <= 0:
            raise DataUnavailable(f"Upstox option chain for {symbol} is empty")
        return OptionChain(symbol, spot, now_ist(), expiries, quotes, lot)

    def india_vix(self) -> float | None:
        data = self._data("/market-quote/ltp", {"instrument_key": self.keys["INDIAVIX"]}) or {}
        for value in data.values():
            return float(value.get("last_price") or 0) or None
        return None


# --------------------------------------------------------------------------- Kite
KITE_BASE = "https://api.kite.trade"
KITE_INDEX = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "INDIAVIX": "NSE:INDIA VIX",
}
KITE_QUOTE_LIMIT = 500


class KiteProvider(_BrokerProvider):
    name = "Zerodha Kite (real-time)"

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        api_key = os.environ.get("KITE_API_KEY", "")
        token = load_token("kite", "KITE_ACCESS_TOKEN")
        if not api_key or not token:
            raise DataUnavailable("Kite needs KITE_API_KEY and an access token: run `python -m optionsbot.auth kite`")
        self.session.headers.update({"X-Kite-Version": "3", "Authorization": f"token {api_key}:{token}"})
        self._oi_baseline: dict[tuple[date, str], float] = {}  # Kite has no previous-day OI field

    def _instruments(self) -> list[dict]:
        def fetch():
            text = self._get(f"{KITE_BASE}/instruments/NFO").text
            return [r for r in csv.DictReader(io.StringIO(text)) if r.get("segment") == "NFO-OPT"]
        return self._cached_contracts("__NFO__", fetch)

    def _quote(self, keys: list[str]) -> dict:
        out: dict = {}
        for i in range(0, len(keys), KITE_QUOTE_LIMIT):
            body = self._get(f"{KITE_BASE}/quote", params=[("i", k) for k in keys[i:i + KITE_QUOTE_LIMIT]]).json()
            if body.get("status") != "success":
                raise DataUnavailable(f"Kite quote: {body.get('message') or body}")
            out.update(body.get("data") or {})
        return out

    def _spot(self, symbol: str) -> float:
        key = KITE_INDEX.get(symbol, f"NSE:{symbol}")
        body = self._get(f"{KITE_BASE}/quote/ltp", params={"i": key}).json()
        try:
            return float(body["data"][key]["last_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataUnavailable(f"Kite returned no price for {key}") from exc

    def option_chain(self, symbol: str) -> OptionChain:
        spot = self._spot(symbol)
        today = now_ist().date()
        contracts = [r for r in self._instruments() if r["name"] == symbol and r["expiry"] >= today.isoformat()]
        if not contracts:
            raise DataUnavailable(f"No NFO option contracts for {symbol} in the Kite instrument list")
        expiries = sorted({date.fromisoformat(r["expiry"]) for r in contracts})
        wanted = set(expiries[: self.cfg.data.max_expiries])
        lot = int(float(contracts[0]["lot_size"])) or self.cfg.lot_sizes.get(symbol, 0)
        strikes = sorted({float(r["strike"]) for r in contracts if date.fromisoformat(r["expiry"]) in wanted})
        near = sorted(strikes, key=lambda k: abs(k - spot))[: 2 * self.cfg.data.strikes_each_side + 1]
        selected = [r for r in contracts if date.fromisoformat(r["expiry"]) in wanted and float(r["strike"]) in near]
        data = self._quote([f"NFO:{r['tradingsymbol']}" for r in selected])
        quotes = []
        for r in selected:
            q = data.get(f"NFO:{r['tradingsymbol']}")
            if not q:
                continue
            depth = q.get("depth") or {}
            bid = float(((depth.get("buy") or [{}])[0]).get("price") or 0)
            ask = float(((depth.get("sell") or [{}])[0]).get("price") or 0)
            oi = self._oi(float(q.get("oi") or 0), lot)
            # OI change is measured from the first reading this session (Kite has no previous-day OI).
            oi_open = self._oi_baseline.setdefault((today, r["tradingsymbol"]), oi)
            quotes.append(OptionQuote(
                strike=float(r["strike"]), option_type=r["instrument_type"], expiry=date.fromisoformat(r["expiry"]),
                ltp=float(q.get("last_price") or 0), bid=bid, ask=ask, iv=0.0,  # IV backed out from mid
                oi=oi, oi_change=oi - oi_open if oi_open else 0.0, volume=float(q.get("volume") or 0),
            ))
        if not quotes:
            raise DataUnavailable(f"Kite returned no option quotes for {symbol}")
        return OptionChain(symbol, spot, now_ist(), expiries, quotes, lot)

    def india_vix(self) -> float | None:
        try:
            return self._spot("INDIAVIX")
        except DataUnavailable:
            return None
