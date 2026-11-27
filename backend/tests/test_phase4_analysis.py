import math
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.core.clock import IST
from backend.indicators.core import atr, ema, macd, relative_volume, rsi, sma, vwap
from backend.market_data.instruments import OptionUniverse
from backend.models.domain import Bar, DepthLevel, OptType, Quote
from backend.options.greeks import bs_delta, bs_price, implied_vol
from backend.options.selection import round_to_tick, select_contract
from backend.strategies.config import StrategyConfig
from backend.strategies.orb import analyse
from backend.tests.fakes import ist_dt, opt

prices = st.lists(st.floats(min_value=1, max_value=1e5, allow_nan=False), min_size=1, max_size=120)


# --- indicators: reference values -------------------------------------------------
def test_sma_ema_reference():
    v = [1, 2, 3, 4, 5, 6]
    assert sma(v, 3) == [None, None, 2, 3, 4, 5]
    e = ema(v, 3)  # seed 2, alpha .5
    assert e[:2] == [None, None] and e[2] == 2 and e[3] == 3 and e[4] == 4 and e[5] == 5


def test_rsi_extremes_and_reference():
    assert rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert rsi(list(range(30, 1, -1)), 14)[-1] == 0.0
    # Hand-computed: alternating +2/-1 for 14 changes -> avg gain 1.0, avg loss 0.5 -> RS 2 -> RSI 66.67
    closes = [100.0]
    for i in range(14):
        closes.append(closes[-1] + (2 if i % 2 == 0 else -1))
    assert rsi(closes, 14)[-1] == pytest.approx(100 - 100 / 3)


def test_macd_constant_is_zero():
    line, sig, hist = macd([50.0] * 60)
    assert line[-1] == 0 and sig[-1] == 0 and hist[-1] == 0


def _bars(closes, vol=100):
    t = ist_dt(2030, 1, 1, 9, 15)
    return [Bar(t + timedelta(minutes=i), c, c + 1, c - 1, c, vol) for i, c in enumerate(closes)]


def test_atr_vwap_rvol():
    b = _bars([100.0] * 20)
    assert atr(b, 14)[-1] == pytest.approx(2.0)
    assert vwap(b)[-1] == pytest.approx(100.0)
    b2 = _bars([100.0] * 21)
    b2[-1] = Bar(b2[-1].ts, 100, 101, 99, 100, 300)
    assert relative_volume(b2, 20)[-1] == pytest.approx(3.0)


def test_vwap_resets_each_session():
    d1 = _bars([100.0] * 3)
    d2 = [Bar(b.ts + timedelta(days=1), 200, 201, 199, 200, 100) for b in d1]
    assert vwap(d1 + d2)[-1] == pytest.approx(200.0)


@settings(max_examples=60, deadline=None)
@given(prices, st.integers(min_value=0, max_value=50))
def test_no_lookahead_indicators(values, cut):
    """Appending future data never changes earlier indicator values."""
    cut = min(cut, len(values))
    full_bars, part_bars = _bars(values), _bars(values[:cut])
    for fn in (lambda v: ema(v, 9), lambda v: rsi(v, 14), lambda v: macd(v)[2], lambda v: sma(v, 5)):
        assert fn(values)[:cut] == fn(values[:cut])
    for fn in (lambda b: atr(b, 14), vwap, lambda b: relative_volume(b, 20)):
        assert fn(full_bars)[:cut] == fn(part_bars)


# --- greeks ----------------------------------------------------------------------
def test_put_call_parity_and_iv_roundtrip():
    s, k, t, r, v = 24000, 24050, 5 / 365, 0.065, 0.14
    c, p = bs_price(OptType.CE, s, k, t, r, v), bs_price(OptType.PE, s, k, t, r, v)
    assert c - p == pytest.approx(s - k * math.exp(-r * t), abs=1e-6)
    assert implied_vol(OptType.CE, c, s, k, t, r) == pytest.approx(v, abs=1e-4)
    assert 0.4 < bs_delta(OptType.CE, s, k, t, r, v) < 0.6
    assert implied_vol(OptType.CE, 0.0001, s, 20000, t, r) is None  # below intrinsic


def test_round_to_tick():
    assert round_to_tick(Decimal("100.52"), Decimal("0.05"), up=True) == Decimal("100.55")
    assert round_to_tick(Decimal("100.52"), Decimal("0.05"), up=False) == Decimal("100.50")


# --- contract selection -------------------------------------------------------------
NOW = datetime(2030, 1, 3, 10, 30, tzinfo=IST)
EXP = date(2030, 1, 8)


def _q(token, mid, spread=0.5, vol=500000, oi=200000, age=1, size=1300):
    m = Decimal(str(mid))
    h = Decimal(str(spread)) / 2
    return Quote(instrument_token=token, last_price=m, ts=NOW - timedelta(seconds=age),
                 last_trade_time=NOW - timedelta(seconds=age), volume=vol, oi=oi,
                 bids=(DepthLevel(m - h, size),), asks=(DepthLevel(m + h, size),))


def _universe():
    return OptionUniverse.build([opt(i, 23800 + 50 * i, k, EXP) for i in range(9) for k in ("CE", "PE")]
                                + [opt(100 + i, 23800 + 50 * i, "CE", date(2030, 1, 15)) for i in range(9)], "NIFTY")


def _price(k, opt_type=OptType.CE, spot=24010):
    from backend.options.selection import years_to_expiry
    return round(bs_price(opt_type, spot, k, years_to_expiry(NOW, EXP), 0.065, 0.13), 2)


def test_selects_atm_when_liquid():
    u = _universe()
    cfg = StrategyConfig().contract
    atm = u.contract(EXP, Decimal(24000), OptType.CE)
    itm = u.contract(EXP, Decimal(23950), OptType.CE)
    quotes = {atm.instrument_token: _q(atm.instrument_token, _price(24000)),
              itm.instrument_token: _q(itm.instrument_token, _price(23950))}
    s = select_contract(u, OptType.CE, 24010, NOW, quotes, cfg)
    assert s.ok and s.instrument == atm and s.liquidity_points == 10


def test_rejects_wide_spread_stale_and_illiquid():
    u = _universe()
    cfg = StrategyConfig().contract
    atm = u.contract(EXP, Decimal(24000), OptType.CE)
    itm = u.contract(EXP, Decimal(23950), OptType.CE)
    quotes = {atm.instrument_token: _q(atm.instrument_token, _price(24000), spread=3),
              itm.instrument_token: _q(itm.instrument_token, _price(23950), age=60)}
    s = select_contract(u, OptType.CE, 24010, NOW, quotes, cfg)
    assert not s.ok
    text = " ".join(s.reasons)
    assert "spread" in text and "stale quote" in text


def test_never_otm_for_pe():
    u = _universe()
    from backend.options.selection import candidate_strikes
    ks = candidate_strikes(u, EXP, 24010, OptType.PE, 1)
    assert ks == [Decimal(24000), Decimal(24050)]  # ATM, then ITM (above spot) for puts


# --- ORB analysis ----------------------------------------------------------------
def _session(day: date, base: float, pattern, vol=1000):
    out = []
    t = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    price = base
    for i in range(375):
        nxt = base + pattern(i)
        v = vol * (4 if pattern is not None and 75 <= i < 80 else 1)
        out.append(Bar(t + timedelta(minutes=i), price, max(price, nxt) + 1, min(price, nxt) - 1, nxt, v))
        price = nxt
    return out


def _history():
    days = [date(2030, 1, 1) + timedelta(days=i) for i in range(3)]
    bars = []
    for n, d in enumerate(days[:-1]):
        bars += _session(d, 24000 + 30 * n, lambda i: i * 0.08)  # gentle uptrend
    # today: flat range 09:15-09:30, then breakout up from 10:30 with volume
    def today(i):
        if i < 75:
            return 60 + (i % 3) * 2.0
        return 60 + (i - 74) * 6.0
    bars += _session(days[-1], 24000, today)
    return bars, days[-1]


def test_orb_bullish_breakout_detected():
    bars, d = _history()
    now = datetime(d.year, d.month, d.day, 10, 40, tzinfo=IST)
    a = analyse(bars, now, StrategyConfig().signal)
    assert a.direction is OptType.CE, (a.vetoes, a.notes)
    assert a.components["breakout"] == 15 and a.components["vwap"] == 15
    assert a.invalidation is not None and a.invalidation < a.spot


def test_orb_no_trade_before_range_complete():
    bars, d = _history()
    a = analyse(bars, datetime(d.year, d.month, d.day, 9, 25, tzinfo=IST), StrategyConfig().signal)
    assert a.direction is None and "opening range not complete" in a.vetoes


def test_orb_ignores_future_bars():
    """Decision at time t is identical whether or not later bars are present."""
    bars, d = _history()
    now = datetime(d.year, d.month, d.day, 10, 40, tzinfo=IST)
    cut = [b for b in bars if b.ts + timedelta(minutes=1) <= now]
    a1, a2 = analyse(bars, now, StrategyConfig().signal), analyse(cut, now, StrategyConfig().signal)
    assert (a1.direction, a1.score_ex_liquidity, a1.vetoes, a1.features) == \
        (a2.direction, a2.score_ex_liquidity, a2.vetoes, a2.features)


def test_orb_gap_veto():
    bars, d = _history()
    shifted = [b if b.ts.date() != d else Bar(b.ts, b.open + 600, b.high + 600, b.low + 600, b.close + 600, b.volume)
               for b in bars]
    a = analyse(shifted, datetime(d.year, d.month, d.day, 10, 40, tzinfo=IST), StrategyConfig().signal)
    assert any("gap" in v for v in a.vetoes)


def test_orb_data_gap_in_opening_range():
    bars, d = _history()
    holed = [b for b in bars if not (b.ts.date() == d and b.ts.minute == 20 and b.ts.hour == 9)]
    a = analyse(holed, datetime(d.year, d.month, d.day, 10, 40, tzinfo=IST), StrategyConfig().signal)
    assert any("data gap" in v for v in a.vetoes)
