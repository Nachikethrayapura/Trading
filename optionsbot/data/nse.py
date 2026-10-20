"""Live data from NSE's public website APIs (option chain, India VIX) plus Yahoo price history.

NSE's endpoints are unofficial: they need browser-like headers and session cookies obtained by
visiting the site first, they rate-limit aggressively, and they change shape occasionally. This
client supports both the legacy `option-chain-indices` endpoint and the newer
`option-chain-contract-info` + `option-chain-v3` pair, and parses fields defensively.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime

import requests

from ..config import Config
from ..models import IST, INDEX_SYMBOLS, Bar, OptionChain, OptionQuote, is_market_open, now_ist
from .base import DataUnavailable, MarketDataProvider
from .yahoo import YahooClient

log = logging.getLogger(__name__)

BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/option-chain",
}
COOKIE_TTL = 240  # seconds
MARKET_STATUS_TTL = 120


def _parse_date(value: str) -> date:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {value!r}")


def _parse_timestamp(value: str | None) -> datetime:
    if value:
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S"):
            try:
                return datetime.strptime(value.strip(), fmt).replace(tzinfo=IST)
            except ValueError:
                continue
    return now_ist()


def _num(d: dict, *keys: str) -> float:
    for key in keys:
        value = d.get(key)
        if value not in (None, "", "-"):
            try:
                return float(str(value).replace(",", ""))
            except ValueError:
                continue
    return 0.0


def parse_quote(raw: dict, option_type: str, strike: float, expiry: date) -> OptionQuote:
    return OptionQuote(
        strike=strike, option_type=option_type, expiry=expiry,
        ltp=_num(raw, "lastPrice", "ltp"),
        bid=_num(raw, "bidprice", "bidPrice", "buyPrice1"),
        ask=_num(raw, "askPrice", "askprice", "sellPrice1"),
        iv=_num(raw, "impliedVolatility", "iv") / 100.0,
        oi=_num(raw, "openInterest", "oi"),
        oi_change=_num(raw, "changeinOpenInterest", "changeInOI"),
        volume=_num(raw, "totalTradedVolume", "volume"),
    )


def parse_chain(payload: dict, symbol: str, lot_size: int, fallback_expiry: date | None = None) -> OptionChain:
    records = payload.get("records") or {}
    rows = records.get("data") or (payload.get("filtered") or {}).get("data") or []
    if not rows:
        raise DataUnavailable(f"NSE option chain for {symbol} is empty")
    quotes: list[OptionQuote] = []
    spot = _num(records, "underlyingValue")
    for row in rows:
        strike = _num(row, "strikePrice")
        raw_expiry = row.get("expiryDate") or row.get("expiryDates")
        for option_type in ("CE", "PE"):
            raw = row.get(option_type)
            if not raw:
                continue
            exp_value = raw.get("expiryDate") or raw_expiry
            expiry = _parse_date(exp_value) if exp_value else fallback_expiry
            if expiry is None:
                continue
            quotes.append(parse_quote(raw, option_type, strike, expiry))
            spot = spot or _num(raw, "underlyingValue")
    if spot <= 0:
        raise DataUnavailable(f"NSE option chain for {symbol} has no underlying value")
    expiries = sorted({q.expiry for q in quotes})
    return OptionChain(symbol, spot, _parse_timestamp(records.get("timestamp")), expiries, quotes, lot_size)


class NSEClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._primed_at = 0.0

    def _prime(self) -> None:
        self.session.cookies.clear()
        self.session.get(f"{BASE}/option-chain", timeout=self.timeout,
                         headers={"Accept": "text/html,application/xhtml+xml"})
        self._primed_at = _time.monotonic()

    def get_json(self, path: str, params: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if attempt or _time.monotonic() - self._primed_at > COOKIE_TTL:
                    self._prime()
                resp = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout)
                if resp.status_code in (401, 403):
                    raise DataUnavailable(f"NSE refused {path} ({resp.status_code})")
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data:
                    return data
                raise DataUnavailable(f"NSE returned an empty payload for {path}")
            except (requests.RequestException, ValueError, DataUnavailable) as exc:
                last_error = exc
        raise DataUnavailable(str(last_error))


class NSEProvider(MarketDataProvider):
    name = "NSE (live, public)"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.nse = NSEClient(cfg.data.request_timeout)
        self.yahoo = YahooClient(cfg.data.request_timeout)
        self._use_v3: dict[str, bool] = {}
        self._status: tuple[float, bool] | None = None

    def _lot_size(self, symbol: str) -> int:
        lot = self.cfg.lot_sizes.get(symbol)
        if not lot:
            raise DataUnavailable(f"Lot size for {symbol} missing: add it under lot_sizes in the config")
        return lot

    def option_chain(self, symbol: str) -> OptionChain:
        lot = self._lot_size(symbol)
        is_index = symbol in INDEX_SYMBOLS
        if not self._use_v3.get(symbol):
            path = "/api/option-chain-indices" if is_index else "/api/option-chain-equities"
            try:
                return parse_chain(self.nse.get_json(path, {"symbol": symbol}), symbol, lot)
            except DataUnavailable as exc:
                log.info("Legacy NSE chain unavailable for %s (%s); switching to v3 API", symbol, exc)
                self._use_v3[symbol] = True
        return self._chain_v3(symbol, lot, "Indices" if is_index else "Equity")

    def _chain_v3(self, symbol: str, lot: int, kind: str) -> OptionChain:
        info = self.nse.get_json("/api/option-chain-contract-info", {"symbol": symbol})
        today = now_ist().date()
        expiries = sorted(d for d in (_parse_date(x) for x in info.get("expiryDates", [])) if d >= today)
        if not expiries:
            raise DataUnavailable(f"No expiries listed for {symbol}")
        chains = []
        for expiry in expiries[: self.cfg.data.max_expiries]:
            payload = self.nse.get_json("/api/option-chain-v3", {
                "type": kind, "symbol": symbol, "expiry": expiry.strftime("%d-%b-%Y")})
            chains.append(parse_chain(payload, symbol, lot, fallback_expiry=expiry))
        quotes = [q for c in chains for q in c.quotes]
        return OptionChain(symbol, chains[0].spot, chains[0].timestamp, expiries, quotes, lot)

    def intraday_bars(self, symbol: str) -> list[Bar]:
        return self.yahoo.bars(symbol, "5m", "5d")

    def daily_bars(self, symbol: str) -> list[Bar]:
        return self.yahoo.bars(symbol, "1d", "1y")

    def market_open(self) -> bool:
        """NSE's own market status, so trading holidays read as closed; falls back to the clock."""
        if self._status and _time.monotonic() - self._status[0] < MARKET_STATUS_TTL:
            return self._status[1]
        is_open = is_market_open(now_ist())
        try:
            for market in self.nse.get_json("/api/marketStatus").get("marketState", []):
                if market.get("market") == "Capital Market":
                    is_open = str(market.get("marketStatus", "")).lower() == "open"
                    break
        except DataUnavailable as exc:
            log.info("NSE market status unavailable (%s); using the clock", exc)
        self._status = (_time.monotonic(), is_open)
        return is_open

    def india_vix(self) -> float | None:
        try:
            data = self.nse.get_json("/api/allIndices")
            for row in data.get("data", []):
                if str(row.get("index", "")).upper() == "INDIA VIX":
                    return _num(row, "last")
        except DataUnavailable as exc:
            log.info("India VIX from NSE unavailable (%s); using Yahoo", exc)
        try:
            return self.yahoo.bars("INDIAVIX", "1d", "5d")[-1].close
        except DataUnavailable:
            return None

    def vix_history(self) -> list[float]:
        return [b.close for b in self.yahoo.bars("INDIAVIX", "1d", "1y")]
