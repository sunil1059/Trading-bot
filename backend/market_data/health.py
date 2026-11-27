"""Data quality and freshness tracking. If data is not healthy, no new trades open."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.core.clock import Clock, to_ist


@dataclass
class DataHealth:
    clock: Clock
    max_tick_age: timedelta = timedelta(seconds=5)
    max_clock_skew: timedelta = timedelta(seconds=5)
    last_tick: dict[int, datetime] = field(default_factory=dict)
    connected: bool = False
    reconnects: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    future_ticks: int = 0
    _seen: dict[int, tuple[datetime, float]] = field(default_factory=dict)

    def on_connect(self) -> None:
        self.connected = True

    def on_disconnect(self) -> None:
        self.connected = False

    def on_reconnect(self) -> None:
        self.reconnects += 1

    def record(self, token: int, ts: datetime, price: float) -> bool:
        """Return True if the tick is usable."""
        ts = to_ist(ts)
        now = self.clock.now()
        if ts - now > self.max_clock_skew:
            self.future_ticks += 1
            return False
        prev = self._seen.get(token)
        if prev is not None:
            if ts < prev[0]:
                self.out_of_order += 1
                return False
            if (ts, price) == prev:
                self.duplicates += 1
                return False
        self._seen[token] = (ts, price)
        self.last_tick[token] = ts
        return True

    def is_fresh(self, token: int) -> bool:
        ts = self.last_tick.get(token)
        return ts is not None and self.clock.now() - ts <= self.max_tick_age

    def problems(self, tokens: list[int]) -> list[str]:
        issues = []
        if not self.connected:
            issues.append("market data stream disconnected")
        for t in tokens:
            if not self.is_fresh(t):
                issues.append(f"stale or missing ticks for {t}")
        return issues
