"""Time handling. All trading logic uses timezone-aware Asia/Kolkata datetimes.

A ``Clock`` is injected everywhere so tests and backtests can control time.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(IST)


class FixedClock:
    """Manually advanced clock for tests and backtests."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start.astimezone(IST)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("clock cannot move backwards")
        self._now += delta

    def set(self, when: datetime) -> None:
        when = when.astimezone(IST)
        if when < self._now:
            raise ValueError("clock cannot move backwards")
        self._now = when


def ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: refusing to guess its timezone")
    return dt.astimezone(IST)
