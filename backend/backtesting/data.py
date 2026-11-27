"""Loading historical bars for backtests.

Real data only: CSV exported from Kite historical API (``scripts/run_backtest.py --fetch``)
or any 1-minute OHLCV CSV with columns ts,open,high,low,close,volume (ts ISO-8601 with
offset, or naive = IST). The loader validates and REFUSES bad data instead of repairing it.
"""

from __future__ import annotations

import csv
from datetime import datetime, time
from pathlib import Path

from backend.core.clock import IST
from backend.models.domain import Bar

SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)


class BadData(ValueError):
    pass


def load_csv(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with Path(path).open(newline="") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            try:
                ts = datetime.fromisoformat(row["ts"])
                ts = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)
                b = Bar(ts, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                        int(float(row.get("volume") or 0)))
            except (KeyError, ValueError) as e:
                raise BadData(f"line {n}: {e}") from e
            bars.append(b)
    validate(bars)
    return bars


def validate(bars: list[Bar]) -> None:
    prev = None
    for b in bars:
        if not (b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close)) or b.low <= 0:
            raise BadData(f"{b.ts}: inconsistent OHLC")
        if b.volume < 0:
            raise BadData(f"{b.ts}: negative volume")
        if not SESSION_OPEN <= b.ts.time() < SESSION_CLOSE:
            raise BadData(f"{b.ts}: outside market hours")
        if prev is not None and b.ts <= prev.ts:
            raise BadData(f"{b.ts}: duplicate or out-of-order bar")
        prev = b
    if bars and all(b.volume == 0 for b in bars):
        raise BadData("no volume in data: use the index FUTURE series for analysis, not the index")


def write_csv(bars: list[Bar], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])
