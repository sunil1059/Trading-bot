"""SYNTHETIC market data for TESTS ONLY. Never used by paper/live/backtest reports."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from backend.core.clock import IST
from backend.models.domain import Bar


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def synthetic_sessions(start: date, n_days: int, seed: int = 7, s0: float = 24000.0) -> list[Bar]:
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not crypto
    bars, price = [], s0
    for k, d in enumerate(trading_days(start, n_days)):
        drift = rng.choice([-1, 1]) * rng.uniform(0, 0.8) if k % 3 else 0.0  # some trend days
        price *= 1 + rng.uniform(-0.003, 0.003)  # overnight gap
        t = datetime.combine(d, time(9, 15), tzinfo=IST)
        for m in range(375):
            o = price
            price = max(100.0, price + drift + rng.gauss(0, 6))
            hi, lo = max(o, price) + abs(rng.gauss(0, 2)), min(o, price) - abs(rng.gauss(0, 2))
            vol = int(rng.uniform(800, 1500) * (2.5 if m < 30 or abs(price - o) > 10 else 1))
            bars.append(Bar(t + timedelta(minutes=m), o, hi, lo, price, vol))
    return bars
