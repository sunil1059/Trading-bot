"""Opening-range breakout analysis and 0-100 confidence scoring.

Input: completed 1-minute bars of the underlying (several sessions, oldest first) up to
``now``. Everything is computed from bars whose interval ended at or before ``now``.

The score ranks setup quality. It is NOT a probability of profit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.core.clock import to_ist
from backend.indicators.core import atr, ema, macd, relative_volume, rsi, vwap
from backend.market_data.candles import resample
from backend.models.domain import Bar, OptType
from backend.strategies.config import SignalParams

WEIGHTS = {"trend": 20, "vwap": 15, "volume": 15, "breakout": 15, "higher_tf": 15, "momentum": 10, "liquidity": 10}


@dataclass
class Analysis:
    ts: datetime
    direction: OptType | None
    score_ex_liquidity: int
    components: dict[str, int] = field(default_factory=dict)
    vetoes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    spot: float | None = None
    invalidation: float | None = None  # underlying level that invalidates the trade
    next_level: float | None = None  # nearest level in the trade direction
    features: dict[str, float | None] = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.direction is not None and not self.vetoes

    def total_score(self, liquidity_points: int) -> int:
        return min(100, self.score_ex_liquidity + liquidity_points)


def _completed(bars: list[Bar], now: datetime) -> list[Bar]:
    return [b for b in bars if b.ts + timedelta(minutes=1) <= now]


def analyse(bars_1m: list[Bar], now: datetime, p: SignalParams, vix: float | None = None,
            event_day: bool = False) -> Analysis:
    now = to_ist(now)
    bars = _completed(bars_1m, now)
    a = Analysis(ts=now, direction=None, score_ex_liquidity=0)
    if not bars or bars[-1].ts.date() != now.date():
        a.vetoes.append("no completed bars for today")
        return a
    today = [b for b in bars if b.ts.date() == now.date()]
    prior = [b for b in bars if b.ts.date() < now.date()]
    if not prior:
        a.vetoes.append("no previous session data")
        return a

    # --- day context ---
    prev_day = [b for b in prior if b.ts.date() == prior[-1].ts.date()]
    pdh, pdl, pdc = max(b.high for b in prev_day), min(b.low for b in prev_day), prev_day[-1].close
    day_open = today[0].open
    gap_pct = (day_open / pdc - 1) * 100
    a.features.update(pdh=pdh, pdl=pdl, pdc=pdc, gap_pct=gap_pct, vix=vix)
    if abs(gap_pct) > p.max_gap_pct:
        a.vetoes.append(f"gap {gap_pct:.2f}% exceeds {p.max_gap_pct}%")
    if vix is not None and p.max_vix is not None and vix > p.max_vix:
        a.vetoes.append(f"India VIX {vix:.1f} above {p.max_vix}")
    if event_day:
        a.vetoes.append("scheduled event day")

    # --- opening range (must be complete and gap-free) ---
    or_end = now.replace(hour=p.opening_range_end.hour, minute=p.opening_range_end.minute, second=0, microsecond=0)
    or_bars = [b for b in today if b.ts < or_end]
    expected = int((or_end - today[0].ts.replace(hour=9, minute=15)).total_seconds() // 60)
    if now < or_end:
        a.vetoes.append("opening range not complete")
        return a
    if len(or_bars) != expected:
        a.vetoes.append(f"opening range has {len(or_bars)}/{expected} bars (data gap)")
        return a
    orh, orl = max(b.high for b in or_bars), min(b.low for b in or_bars)

    # --- signal timeframe ---
    sig = resample(bars, p.signal_minutes, now=now)
    sig_today = [b for b in sig if b.ts.date() == now.date()]
    if len(sig) < 60 or not sig_today:
        a.vetoes.append("not enough signal-timeframe history")
        return a
    closes = [b.close for b in sig]
    e9, e21, e50 = ema(closes, 9)[-1], ema(closes, 21)[-1], ema(closes, 50)[-1]
    r = rsi(closes, 14)[-1]
    _, _, hist = macd(closes)
    h = hist[-1]
    at = atr(sig, 14)[-1]
    vw = vwap(bars)[-1]  # session VWAP from 1-minute bars
    rv = relative_volume(sig, p.rvol_lookback)[-1]
    last = sig[-1]
    spot = last.close
    a.spot = spot
    a.features.update(orh=orh, orl=orl, ema9=e9, ema21=e21, ema50=e50, rsi=r, macd_hist=h, atr=at, vwap=vw,
                      rvol=rv, close=spot, today_high=max(b.high for b in today), today_low=min(b.low for b in today))
    if None in (e9, e21, e50, r, h, at, vw, rv):
        a.vetoes.append("indicators not warmed up")
        return a

    # --- higher timeframe ---
    htf = resample(bars, p.trend_minutes, now=now)
    hc = [b.close for b in htf]
    h9, h21, h50 = ema(hc, 9)[-1], ema(hc, 21)[-1], ema(hc, 50)[-1]
    a.features.update(htf_ema9=h9, htf_ema21=h21, htf_ema50=h50)
    if None in (h9, h21, h50):
        a.vetoes.append("higher timeframe not warmed up")
        return a

    # --- breakout freshness: a close beyond the range within the last N signal bars,
    #     after a close inside it ---
    def fresh_break(up: bool) -> bool:
        recent = [b for b in sig_today if b.ts >= or_end][-(p.breakout_max_age_bars + 1):]
        if not recent:
            return False
        beyond = (lambda c: c > orh) if up else (lambda c: c < orl)
        if not beyond(recent[-1].close):
            return False
        before = [b for b in sig_today if b.ts >= or_end]
        idx = len(before) - 1
        first_beyond = idx
        while first_beyond > 0 and beyond(before[first_beyond - 1].close):
            first_beyond -= 1
        return idx - first_beyond < p.breakout_max_age_bars

    up, down = fresh_break(True), fresh_break(False)
    if up == down:
        a.notes.append("no fresh opening-range breakout")
        return a
    opt = OptType.CE if up else OptType.PE
    a.direction = opt
    bull = opt is OptType.CE
    c: dict[str, int] = {}

    def side(x: float, y: float) -> bool:
        return x > y if bull else x < y

    c["trend"] = (10 if side(e9, e21) else 0) + (10 if side(e21, e50) else 0)
    c["vwap"] = WEIGHTS["vwap"] if side(spot, vw) else 0
    c["volume"] = 15 if rv >= p.min_relative_volume else (7 if rv >= 1.2 else 0)
    c["breakout"] = WEIGHTS["breakout"]
    c["higher_tf"] = (8 if side(h9, h21) else 0) + (7 if side(htf[-1].close, h50) else 0)
    lo, hi = p.rsi_ce_band if bull else p.rsi_pe_band
    c["momentum"] = (6 if lo <= r <= hi else 0) + (4 if side(h, 0.0) else 0)
    a.components = c
    a.score_ex_liquidity = sum(c.values())
    if not side(spot, vw):
        a.vetoes.append("price on the wrong side of VWAP")

    # --- invalidation and room to run ---
    buf = p.invalidation_atr_buffer * at
    a.invalidation = orh - buf if bull else orl + buf
    stop_dist = abs(spot - a.invalidation)
    # Levels that existed BEFORE the current signal bar (the bar's own high is not resistance).
    before = [b for b in today if b.ts < last.ts]
    levels = [pdh, pdl] + ([max(b.high for b in before), min(b.low for b in before)] if before else [])
    ahead = [lv for lv in levels if (lv > spot if bull else lv < spot)]
    a.next_level = (min(ahead) if bull else max(ahead)) if ahead else None
    if a.next_level is not None and stop_dist > 0:
        room = abs(a.next_level - spot)
        a.features["room_ratio"] = room / stop_dist
        if room < p.min_room_to_stop_ratio * stop_dist:
            a.vetoes.append(f"only {room / stop_dist:.2f}x room to next level")
    return a
