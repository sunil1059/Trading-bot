"""Performance metrics shared by the dashboard and the backtester."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class TradeResult:
    entry_time: datetime
    exit_time: datetime
    net_pnl: float
    exit_reason: str = ""


def compute_metrics(trades: Sequence[TradeResult], starting_capital: float) -> dict[str, float | int | None]:
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    equity, peak, max_dd = starting_capital, starting_capital, 0.0
    streak = max_streak = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        streak = streak + 1 if p <= 0 else 0
        max_streak = max(max_streak, streak)
    daily: dict[date, float] = defaultdict(float)
    for t in trades:
        daily[t.exit_time.date()] += t.net_pnl
    d = list(daily.values())
    sharpe = None
    if len(d) > 1:
        mu = sum(d) / len(d)
        sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (len(d) - 1))
        sharpe = (mu / sd) * math.sqrt(252) if sd > 0 else None
    hold = [(t.exit_time - t.entry_time).total_seconds() / 60 for t in trades]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "total_trades": len(pnls),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / len(pnls) if pnls else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd / starting_capital if starting_capital else None,
        "max_consecutive_losses": max_streak,
        "daily_sharpe_annualised": sharpe,
        "avg_holding_minutes": sum(hold) / len(hold) if hold else None,
        "trading_days": len(daily),
    }
