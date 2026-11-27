"""Risk engine: the gate between signals and orders. It cannot be bypassed.

Every entry goes through ``check_entry``; the execution layer refuses orders without an
approved ``RiskDecision``. State is serialisable so it survives restarts.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from backend.core.config import RiskConfig
from backend.models.domain import OptType
from backend.risk.charges import ChargeRates, round_trip_charges

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    day: date
    week: str  # ISO year-week, e.g. 2030-W01
    realised_pnl_today: Decimal = Decimal(0)
    open_mtm: Decimal = Decimal(0)
    realised_pnl_week: Decimal = Decimal(0)
    trades_today: int = 0
    consecutive_losses: int = 0
    paused_until: datetime | None = None
    day_lock_reason: str | None = None
    week_lock_reason: str | None = None
    emergency_lock: bool = False
    last_stop_out: dict[str, datetime] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = str(v)
            elif isinstance(v, (datetime, date)):
                d[k] = v.isoformat()
        d["last_stop_out"] = {k: v.isoformat() for k, v in self.last_stop_out.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RiskState:
        return cls(
            day=date.fromisoformat(d["day"]), week=d["week"],
            realised_pnl_today=Decimal(d["realised_pnl_today"]), open_mtm=Decimal(d["open_mtm"]),
            realised_pnl_week=Decimal(d["realised_pnl_week"]), trades_today=int(d["trades_today"]),
            consecutive_losses=int(d["consecutive_losses"]),
            paused_until=datetime.fromisoformat(d["paused_until"]) if d.get("paused_until") else None,
            day_lock_reason=d.get("day_lock_reason"), week_lock_reason=d.get("week_lock_reason"),
            emergency_lock=bool(d.get("emergency_lock")),
            last_stop_out={k: datetime.fromisoformat(v) for k, v in (d.get("last_stop_out") or {}).items()},
        )


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


@dataclass(frozen=True)
class EntryContext:
    now: datetime
    direction: OptType
    score: int
    entry_price: Decimal  # limit price incl. slippage allowance
    stop_price: Decimal  # option premium stop
    exit_slippage: Decimal  # expected adverse slippage per unit on the stop exit
    lot_size: int
    available_funds: Decimal
    open_positions: int
    pending_entries: int
    has_position_in_contract: bool
    data_problems: tuple[str, ...]
    is_trading_day: bool
    is_expiry_day: bool
    unreconciled_orders: int = 0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    lots: int = 0
    quantity: int = 0
    max_loss: Decimal = Decimal(0)
    capital_required: Decimal = Decimal(0)
    reasons: tuple[str, ...] = ()


class RiskEngine:
    def __init__(self, cfg: RiskConfig, rates: ChargeRates, state: RiskState) -> None:
        self.cfg, self.rates, self.state = cfg, rates, state

    @classmethod
    def fresh(cls, cfg: RiskConfig, rates: ChargeRates, today: date) -> RiskEngine:
        return cls(cfg, rates, RiskState(day=today, week=iso_week(today)))

    # --- session rollover ---------------------------------------------------------
    def roll(self, now: datetime) -> None:
        d = now.date()
        if d != self.state.day:
            s = self.state
            if iso_week(d) != s.week:
                s.week, s.realised_pnl_week, s.week_lock_reason = iso_week(d), Decimal(0), None
            s.day, s.realised_pnl_today, s.open_mtm, s.trades_today = d, Decimal(0), Decimal(0), 0
            s.consecutive_losses, s.paused_until, s.day_lock_reason = 0, None, None
            s.last_stop_out = {}
            # emergency_lock is NOT cleared automatically: it needs a manual reset.

    # --- the gate -----------------------------------------------------------------
    def blocking_reasons(self, now: datetime) -> list[str]:
        s, c = self.state, self.cfg
        r = []
        if s.emergency_lock:
            r.append("emergency stop active (manual reset required)")
        if s.week_lock_reason:
            r.append(f"weekly lock: {s.week_lock_reason}")
        if s.day_lock_reason:
            r.append(f"day lock: {s.day_lock_reason}")
        if s.paused_until and now < s.paused_until:
            r.append(f"paused until {s.paused_until:%H:%M}")
        if s.trades_today >= c.trades.max_trades_per_day:
            r.append("daily trade limit reached")
        t = now.time()
        if t < c.session.first_entry or t > c.session.last_entry:
            r.append("outside entry window")
        return r

    def check_entry(self, ctx: EntryContext) -> RiskDecision:
        self.roll(ctx.now)
        c = self.cfg
        reasons = self.blocking_reasons(ctx.now)
        if not ctx.is_trading_day:
            reasons.append("not a trading day")
        if ctx.is_expiry_day and not c.session.allow_expiry_day_entries:
            reasons.append("expiry day entries disabled")
        if ctx.data_problems:
            reasons.extend(f"data: {p}" for p in ctx.data_problems)
        if ctx.unreconciled_orders:
            reasons.append("unreconciled orders exist")
        if ctx.open_positions + ctx.pending_entries >= c.trades.max_open_positions:
            reasons.append("max open positions reached")
        if ctx.has_position_in_contract:
            reasons.append("duplicate position in this contract")
        if ctx.score < c.min_signal_score_for_entry:
            reasons.append(f"score {ctx.score} below {c.min_signal_score_for_entry}")
        last = self.state.last_stop_out.get(ctx.direction.value)
        if last and ctx.now - last < timedelta(minutes=c.trades.same_direction_reentry_cooldown_minutes):
            reasons.append("re-entry cooldown after stop-out")
        if ctx.stop_price <= 0 or ctx.stop_price >= ctx.entry_price:
            reasons.append("invalid stop: must be below entry and above zero")
        if reasons:
            return RiskDecision(False, reasons=tuple(reasons))

        # --- sizing: whole lots, fixed cap, never scaled by recent results ---
        lot = ctx.lot_size
        per_unit_loss = ctx.entry_price - ctx.stop_price + ctx.exit_slippage
        one_lot_loss = per_unit_loss * lot + round_trip_charges(self.rates, ctx.entry_price, ctx.stop_price, lot)
        one_lot_capital = ctx.entry_price * lot
        remaining_day = Decimal(c.capital.max_daily_loss) + min(self.state.realised_pnl_today + self.state.open_mtm,
                                                                Decimal(0))
        loss_budget = min(Decimal(c.capital.max_loss_per_trade), remaining_day)
        caps = {
            "risk": (loss_budget / one_lot_loss).to_integral_value(ROUND_FLOOR),
            "capital": (Decimal(c.capital.max_capital_per_trade) / one_lot_capital).to_integral_value(ROUND_FLOOR),
            "funds": (ctx.available_funds / one_lot_capital).to_integral_value(ROUND_FLOOR),
            "config": Decimal(c.trades.max_lots_per_trade),
        }
        lots = int(min(caps.values()))
        if lots < 1:
            why = [k for k, v in caps.items() if v < 1]
            return RiskDecision(False, reasons=(
                f"one lot (Rs {one_lot_capital:.0f}, max loss Rs {one_lot_loss:.0f}) "
                f"exceeds limits: {', '.join(why)}",))
        qty = lots * lot
        max_loss = per_unit_loss * qty + round_trip_charges(self.rates, ctx.entry_price, ctx.stop_price, qty)
        return RiskDecision(True, lots=lots, quantity=qty, max_loss=max_loss.quantize(Decimal("0.01")),
                            capital_required=ctx.entry_price * qty)

    # --- events -------------------------------------------------------------------
    def on_entry_filled(self, now: datetime) -> None:
        self.roll(now)
        self.state.trades_today += 1

    def update_mtm(self, open_mtm: Decimal, now: datetime) -> bool:
        """Returns True if the daily loss limit is breached (caller must exit per policy)."""
        self.roll(now)
        self.state.open_mtm = open_mtm
        return self._check_loss_limits()

    def on_trade_closed(self, net_pnl: Decimal, direction: OptType, exit_reason: str, now: datetime) -> list[str]:
        self.roll(now)
        s, c = self.state, self.cfg
        s.realised_pnl_today += net_pnl
        s.realised_pnl_week += net_pnl
        s.open_mtm = Decimal(0)
        events = []
        if net_pnl < 0:
            s.consecutive_losses += 1
            if exit_reason in ("stop_loss", "underlying_invalidation"):
                s.last_stop_out[direction.value] = now
            if s.consecutive_losses >= c.trades.stop_after_consecutive_losses:
                s.day_lock_reason = f"{s.consecutive_losses} consecutive losses"
                events.append("STOPPED_FOR_DAY")
            elif s.consecutive_losses >= c.trades.pause_after_consecutive_losses:
                s.paused_until = now + timedelta(minutes=c.trades.pause_minutes)
                events.append("PAUSED")
        else:
            s.consecutive_losses = 0
        if self._check_loss_limits():
            events.append("LOSS_LIMIT")
        return events

    def _check_loss_limits(self) -> bool:
        s, c = self.state, self.cfg
        breached = False
        if s.realised_pnl_today + s.open_mtm <= -Decimal(c.capital.max_daily_loss):
            s.day_lock_reason = s.day_lock_reason or "daily loss limit reached"
            breached = True
        if s.realised_pnl_week + min(s.open_mtm, Decimal(0)) <= -Decimal(c.capital.max_weekly_loss):
            s.week_lock_reason = s.week_lock_reason or "weekly loss limit reached"
            breached = True
        return breached

    # --- controls -----------------------------------------------------------------
    def emergency_stop(self) -> None:
        self.state.emergency_lock = True

    def manual_reset(self, *, clear_emergency: bool, clear_week: bool = False) -> None:
        """Only via an authenticated, confirmed dashboard action. Never automatic."""
        if clear_emergency:
            self.state.emergency_lock = False
        if clear_week:
            self.state.week_lock_reason = None
        self.state.paused_until = None
