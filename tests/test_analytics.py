import math
from datetime import date, datetime

import pytest

from optionsbot.analytics.chain_metrics import max_pain, oi_walls, pcr
from optionsbot.analytics.greeks import bs_greeks, bs_price, implied_vol
from optionsbot.analytics.indicators import ema, realized_vol, rsi, supertrend, trend_score
from optionsbot.models import IST, Bar, OptionQuote, time_to_expiry


def test_put_call_parity():
    s, k, t, r, v = 25000, 25200, 7 / 365, 0.06, 0.14
    call, put = bs_price(s, k, t, r, v, "CE"), bs_price(s, k, t, r, v, "PE")
    assert call - put == pytest.approx(s - k * math.exp(-r * t), abs=1e-6)


@pytest.mark.parametrize("option_type,strike", [("CE", 25500), ("PE", 24500), ("CE", 25000)])
def test_implied_vol_round_trip(option_type, strike):
    price = bs_price(25000, strike, 10 / 365, 0.06, 0.17, option_type)
    assert implied_vol(price, 25000, strike, 10 / 365, 0.06, option_type) == pytest.approx(0.17, abs=1e-4)


def test_implied_vol_rejects_prices_without_time_value():
    assert implied_vol(0.0, 25000, 25500, 0.02, 0.06, "CE") is None
    assert implied_vol(400.0, 25500, 25000, 0.02, 0.06, "CE") is None  # below intrinsic


def test_greeks_signs_and_bounds():
    call = bs_greeks(25000, 25000, 0.05, 0.06, 0.15, "CE")
    put = bs_greeks(25000, 25000, 0.05, 0.06, 0.15, "PE")
    assert 0.5 < call.delta < 0.6 and -0.5 < put.delta < -0.4
    assert call.delta - put.delta == pytest.approx(1.0)
    assert call.gamma == pytest.approx(put.gamma) and call.gamma > 0
    assert call.theta < 0 and call.vega > 0


def test_time_to_expiry_is_zero_after_close():
    expiry = date(2026, 10, 27)
    assert time_to_expiry(expiry, datetime(2026, 10, 27, 15, 31, tzinfo=IST)) == 0
    assert time_to_expiry(expiry, datetime(2026, 10, 26, 15, 30, tzinfo=IST)) == pytest.approx(1 / 365)


def _q(strike, option_type, oi, oi_change=0.0, volume=0.0):
    return OptionQuote(strike, option_type, date(2026, 10, 27), oi=oi, oi_change=oi_change, volume=volume)


def test_max_pain_pcr_and_walls():
    quotes = [
        _q(100, "CE", 10), _q(110, "CE", 50), _q(120, "CE", 80),
        _q(90, "PE", 70), _q(100, "PE", 40), _q(110, "PE", 5),
    ]
    # Payout at 100 = calls 0 + puts (110-100)*5 = 50; at 110 = calls 10*10 = 100 + 0; at 90 = puts 400.
    assert max_pain(quotes) == 100
    assert pcr(quotes)[0] == pytest.approx(115 / 140)
    assert oi_walls(quotes, spot=105) == (120, 90)


def test_indicators():
    up = [100 + i for i in range(60)]
    assert ema(up, 10)[-1] < up[-1]
    assert rsi(up) == 100.0
    assert realized_vol([100.0] * 30) == 0.0
    bars = [Bar(datetime(2026, 10, 19, 9, 15, tzinfo=IST), p, p + 1, p - 1, p + 0.5) for p in up]
    assert supertrend(bars) == 1
    score, components = trend_score(bars, [])
    assert score > 0.5 and "ema_cross" in components
