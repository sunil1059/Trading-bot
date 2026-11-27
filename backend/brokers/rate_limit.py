"""Client-side rate limiting and bounded retry with exponential backoff + jitter."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from backend.brokers.base import BrokerError

T = TypeVar("T")


class RateLimiter:
    """Sliding-window limiter: at most ``max_calls`` per ``period`` seconds."""

    def __init__(self, max_calls: int, period: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.max_calls, self.period = max_calls, period
        self._clock, self._sleep = clock, sleep
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._calls = [t for t in self._calls if now - t < self.period]
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0])
            self._sleep(max(wait, 0.001))


def retry_read(fn: Callable[[], T], *, attempts: int = 4, base_delay: float = 0.5, max_delay: float = 8.0,
               sleep: Callable[[float], None] = time.sleep) -> T:
    """Retry an idempotent READ call on retryable broker errors. Never use for order placement."""
    for i in range(attempts):
        try:
            return fn()
        except BrokerError as exc:
            if not exc.retryable or i == attempts - 1:
                raise
            delay = min(max_delay, base_delay * 2**i)
            sleep(delay * (0.5 + random.random() / 2))  # noqa: S311 - jitter, not crypto
    raise AssertionError("unreachable")
