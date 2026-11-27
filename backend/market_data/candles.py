"""Tick -> candle aggregation and timeframe resampling.

Rules that prevent look-ahead and repainting:
- A bar is emitted only after its interval has fully elapsed (by exchange time).
- Consumers only ever see completed bars; the forming bar is never exposed to strategies.
- Higher timeframes are built from completed 1-minute bars aligned to the session open.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta

from backend.core.clock import to_ist
from backend.models.domain import Bar, Tick

SESSION_OPEN = time(9, 15)


def bucket_start(ts: datetime, minutes: int, session_open: time = SESSION_OPEN) -> datetime:
    """Start of the ``minutes`` bucket containing ``ts``, aligned to the session open."""
    ts = to_ist(ts)
    anchor = ts.replace(hour=session_open.hour, minute=session_open.minute, second=0, microsecond=0)
    elapsed = int((ts - anchor).total_seconds() // 60)
    return anchor + timedelta(minutes=(elapsed // minutes) * minutes)


class TickAggregator:
    """Builds 1-minute bars from ticks for one instrument.

    Volume per bar comes from differences in cumulative day volume, so a missed tick
    does not lose volume. Out-of-order or duplicate ticks are dropped and counted.
    """

    def __init__(self, instrument_token: int, minutes: int = 1) -> None:
        self.instrument_token = instrument_token
        self.minutes = minutes
        self._cur: dict | None = None
        self._last_ts: datetime | None = None
        self._last_cum_vol: int | None = None
        self.dropped = 0

    def on_tick(self, tick: Tick) -> list[Bar]:
        if tick.instrument_token != self.instrument_token:
            return []
        ts = to_ist(tick.ts)
        if self._last_ts is not None and ts < self._last_ts:
            self.dropped += 1
            return []
        completed = self.close_until(ts)
        start = bucket_start(ts, self.minutes)
        vol_delta = 0
        if tick.volume_traded is not None:
            if self._last_cum_vol is not None:
                vol_delta = max(0, tick.volume_traded - self._last_cum_vol)
            self._last_cum_vol = tick.volume_traded
        p = tick.last_price
        if self._cur is None:
            self._cur = dict(ts=start, open=p, high=p, low=p, close=p, volume=vol_delta, oi=tick.oi)
        else:
            c = self._cur
            c["high"], c["low"], c["close"] = max(c["high"], p), min(c["low"], p), p
            c["volume"] += vol_delta
            if tick.oi is not None:
                c["oi"] = tick.oi
        self._last_ts = ts
        return completed

    def close_until(self, now: datetime) -> list[Bar]:
        """Emit the forming bar if ``now`` is past its end. Call on a timer too, so a quiet
        instrument still completes its bar."""
        if self._cur is None:
            return []
        end = self._cur["ts"] + timedelta(minutes=self.minutes)
        if to_ist(now) >= end:
            bar = Bar(**self._cur)
            self._cur = None
            return [bar]
        return []


def resample(bars: Iterable[Bar], minutes: int, now: datetime | None = None) -> list[Bar]:
    """Resample completed 1-minute bars into ``minutes`` bars aligned to the session open.

    The last bucket is included only if it is complete: either ``now`` is past its end,
    or (with ``now`` None) it already contains its final minute.
    """
    out: list[Bar] = []
    cur: dict | None = None
    last_minute: datetime | None = None
    for b in bars:
        start = bucket_start(b.ts, minutes)
        if cur is not None and start != cur["ts"]:
            out.append(Bar(**cur))
            cur = None
        if cur is None:
            cur = dict(ts=start, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume, oi=b.oi)
        else:
            cur["high"], cur["low"], cur["close"] = max(cur["high"], b.high), min(cur["low"], b.low), b.close
            cur["volume"] += b.volume
            cur["oi"] = b.oi if b.oi is not None else cur["oi"]
        last_minute = b.ts
    if cur is not None and last_minute is not None:
        end = cur["ts"] + timedelta(minutes=minutes)
        last_end = to_ist(now) if now is not None else last_minute + timedelta(minutes=1)
        complete = last_end >= end
        if complete:
            out.append(Bar(**cur))
    return out
