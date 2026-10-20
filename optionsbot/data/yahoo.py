"""Price history from Yahoo Finance's public chart endpoint (no API key)."""
from __future__ import annotations

from datetime import datetime

import requests

from ..models import IST, Bar, INDEX_SYMBOLS
from .base import DataUnavailable

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
YAHOO_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
    "INDIAVIX": "^INDIAVIX",
}


def yahoo_ticker(symbol: str) -> str:
    if symbol in YAHOO_TICKERS:
        return YAHOO_TICKERS[symbol]
    if symbol in INDEX_SYMBOLS:
        raise DataUnavailable(f"No Yahoo ticker mapping for index {symbol}")
    return f"{symbol}.NS"


class YahooClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

    def bars(self, symbol: str, interval: str, range_: str) -> list[Bar]:
        try:
            resp = self.session.get(CHART_URL.format(ticker=yahoo_ticker(symbol)),
                                    params={"interval": interval, "range": range_}, timeout=self.timeout)
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise DataUnavailable(f"Yahoo {symbol} {interval}: {exc}") from exc
        quote = result["indicators"]["quote"][0]
        bars = []
        for i, ts in enumerate(result.get("timestamp") or []):
            o, h, l, c = (quote[k][i] for k in ("open", "high", "low", "close"))
            if None in (o, h, l, c):
                continue
            bars.append(Bar(datetime.fromtimestamp(ts, IST), o, h, l, c, quote.get("volume", [0])[i] or 0))
        if not bars:
            raise DataUnavailable(f"Yahoo returned no bars for {symbol}")
        return bars
