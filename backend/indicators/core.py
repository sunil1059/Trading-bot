"""Indicator implementations on completed bars only.

Each function returns a list aligned with its input; positions without enough history
are ``None`` (never back-filled). Value at index i depends only on inputs 0..i, which a
property test enforces (no look-ahead, no repainting).

Conventions (match common charting platforms):
- EMA seeded with the SMA of the first ``n`` values, alpha = 2/(n+1).
- RSI and ATR use Wilder smoothing, seeded with simple averages.
- VWAP resets each session (by IST date) and uses typical price (H+L+C)/3.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.models.domain import Bar

Series = list[float | None]


def sma(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n

    def val(g: float, lo: float) -> float:
        if lo == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / lo)

    out[n] = val(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
        avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = val(avg_g, avg_l)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [f - s if f is not None and s is not None else None for f, s in zip(ef, es, strict=True)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: Series = [None] * len(closes)
    if start is not None:
        tail = ema([v for v in line[start:] if v is not None], signal)
        sig[start:] = tail
    hist: Series = [m - s if m is not None and s is not None else None for m, s in zip(line, sig, strict=True)]
    return line, sig, hist


def true_range(bars: Sequence[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: Sequence[Bar], n: int = 14) -> Series:
    tr = true_range(bars)
    out: Series = [None] * len(bars)
    if len(bars) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(bars)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def vwap(bars: Sequence[Bar]) -> Series:
    out: Series = []
    day = None
    pv = vol = 0.0
    for b in bars:
        if b.ts.date() != day:
            day, pv, vol = b.ts.date(), 0.0, 0.0
        tp = (b.high + b.low + b.close) / 3
        pv += tp * b.volume
        vol += b.volume
        out.append(pv / vol if vol > 0 else None)
    return out


def relative_volume(bars: Sequence[Bar], n: int = 20) -> Series:
    """Volume of each bar divided by the average of the previous ``n`` bars (excludes itself)."""
    vols = [float(b.volume) for b in bars]
    out: Series = [None] * len(bars)
    for i in range(n, len(bars)):
        avg = sum(vols[i - n:i]) / n
        out[i] = vols[i] / avg if avg > 0 else None
    return out


def last(series: Series) -> float | None:
    return series[-1] if series else None
