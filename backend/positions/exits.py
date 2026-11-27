"""Exit rules for an open long option position. Pure logic, no I/O.

Order of precedence: emergency > risk lock > data unreliable > square-off > hard stop >
underlying invalidation > target/partial > signal invalidation > time stop.
Stops use the option's BID (the price we could actually sell at).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from backend.core.config import EmergencyExitPolicy
from backend.models.domain import OptType
from backend.strategies.config import ExitParams


@dataclass
class TradePlan:
    direction: OptType
    entry_price: Decimal
    quantity: int
    lot_size: int
    stop: Decimal
    target: Decimal
    invalidation: float | None  # underlying level
    opened_at: datetime
    risk_per_unit: Decimal = field(init=False)
    partial_done: bool = False
    breakeven_set: bool = False
    high_water: Decimal = field(init=False)

    def __post_init__(self) -> None:
        if self.stop >= self.entry_price:
            raise ValueError("stop must be below entry")
        self.risk_per_unit = self.entry_price - self.stop
        self.high_water = self.entry_price

    @classmethod
    def build(cls, direction: OptType, entry: Decimal, quantity: int, lot_size: int, p: ExitParams,
              opened_at: datetime, invalidation: float | None, underlying_stop_premium: Decimal | None = None
              ) -> TradePlan:
        pct_stop = entry * (1 - Decimal(str(p.stop_premium_pct)))
        stop = max(pct_stop, underlying_stop_premium) if underlying_stop_premium is not None else pct_stop
        stop = min(stop, entry - Decimal("0.05"))
        target = entry + (entry - stop) * Decimal(str(p.target_r))
        return cls(direction, entry, quantity, lot_size, stop.quantize(Decimal("0.05")),
                   target.quantize(Decimal("0.05")), invalidation, opened_at)

    def r_multiple(self, price: Decimal) -> Decimal:
        return (price - self.entry_price) / self.risk_per_unit


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    quantity: int
    urgent: bool = False  # urgent exits may escalate to protected MARKET orders


@dataclass(frozen=True)
class MarketView:
    now: datetime
    option_bid: Decimal | None
    spot: float | None
    last_signal_close: float | None = None  # last completed signal-timeframe close
    vwap: float | None = None
    data_ok: bool = True


def evaluate(plan: TradePlan, mv: MarketView, p: ExitParams, square_off: time, *, emergency: bool = False,
             risk_lock: bool = False, emergency_policy: EmergencyExitPolicy = EmergencyExitPolicy.EXIT_ALL,
             expiry_exit: bool = False) -> ExitDecision | None:
    full = plan.quantity
    if full <= 0:
        return None
    if emergency and emergency_policy is EmergencyExitPolicy.EXIT_ALL:
        return ExitDecision("emergency_stop", full, urgent=True)
    if risk_lock and emergency_policy is EmergencyExitPolicy.EXIT_ALL:
        return ExitDecision("daily_risk_limit", full, urgent=True)
    if not mv.data_ok:
        # Unreliable data: we cannot monitor stops, so flatten (policy: protect capital).
        return ExitDecision("data_unreliable", full, urgent=True)
    if mv.now.time() >= square_off:
        return ExitDecision("session_end", full, urgent=True)
    if expiry_exit:
        return ExitDecision("expiry_policy", full, urgent=True)
    bid = mv.option_bid
    if bid is None:
        return ExitDecision("no_bid", full, urgent=True)

    plan.high_water = max(plan.high_water, bid)
    if bid <= plan.stop:
        return ExitDecision("stop_loss" if not plan.breakeven_set else "trailing_stop", full, urgent=True)
    if plan.invalidation is not None and mv.spot is not None:
        bull = plan.direction is OptType.CE
        if (bull and mv.spot < plan.invalidation) or (not bull and mv.spot > plan.invalidation):
            return ExitDecision("underlying_invalidation", full, urgent=True)

    r = plan.r_multiple(bid)
    if bid >= plan.target:
        return ExitDecision("target", full)
    if p.partial_at_r is not None and not plan.partial_done and r >= Decimal(str(p.partial_at_r)):
        lots = plan.quantity // plan.lot_size
        if lots >= 2:
            plan.partial_done = True
            return ExitDecision("partial_target", (lots // 2) * plan.lot_size)
    # Stop management (mutates the plan; no order).
    if not plan.breakeven_set and r >= Decimal(str(p.breakeven_at_r)):
        plan.stop = max(plan.stop, plan.entry_price)
        plan.breakeven_set = True
    if plan.partial_done:
        trail = plan.high_water * (1 - Decimal(str(p.trail_after_partial_pct)))
        plan.stop = max(plan.stop, trail.quantize(Decimal("0.05")))

    if p.exit_on_vwap_cross and mv.last_signal_close is not None and mv.vwap is not None:
        bull = plan.direction is OptType.CE
        if (bull and mv.last_signal_close < mv.vwap) or (not bull and mv.last_signal_close > mv.vwap):
            return ExitDecision("signal_invalidated", full)
    if mv.now - plan.opened_at >= timedelta(minutes=p.time_stop_minutes) and r < Decimal(str(p.time_stop_min_r)) \
            and not plan.breakeven_set:
        return ExitDecision("time_stop", full)
    return None
