"""Trading engine: orchestrates data -> analysis -> risk -> execution -> monitoring.

Runs in PAPER, SIGNAL_ONLY or (behind the LIVE gate) LIVE mode with the same logic.
``step()`` is synchronous and deterministic given its inputs; a runner thread calls it
every few seconds. Every decision is persisted (signals, orders, positions, trades,
risk events) and surfaced as alerts.

Analysis uses the near-month index FUTURE's candles because the index itself carries no
volume (VWAP and volume confirmation need traded volume). The future is used as DATA
ONLY: the bot never trades futures. ATM strikes are chosen from the index spot.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.brokers.base import AuthError, BrokerBase, BrokerError
from backend.core.calendar import CalendarUnavailable, TradingCalendar
from backend.core.clock import Clock
from backend.core.config import RiskConfig, TradingMode
from backend.database.models import (
    AuditLog,
    BrokerEvent,
    Position,
    PositionPlan,
    PositionStatus,
    RiskEvent,
    RiskStateRow,
    Signal,
    SignalStatus,
    Trade,
)
from backend.database.models import OrderStatus as DbOrderStatus
from backend.execution.order_manager import OrderManager
from backend.indicators.core import vwap
from backend.market_data.candles import TickAggregator, resample
from backend.market_data.health import DataHealth
from backend.market_data.instruments import OptionUniverse
from backend.models.domain import Bar, Instrument, OptType, Tick
from backend.notifications.service import Notifier
from backend.options.selection import Selection, round_to_tick, select_contract
from backend.positions.exits import ExitDecision, MarketView, TradePlan, evaluate
from backend.positions.reconcile import reconcile
from backend.risk.charges import ChargeRates, leg_charges
from backend.risk.engine import EntryContext, RiskEngine, RiskState
from backend.strategies.config import StrategyConfig
from backend.strategies.orb import WEIGHTS, Analysis, analyse

log = logging.getLogger(__name__)


class EngineError(RuntimeError):
    pass


class OpenTrade:
    def __init__(self, position_id: int, inst: Instrument, plan: TradePlan, entry_charges: Decimal) -> None:
        self.position_id, self.inst, self.plan = position_id, inst, plan
        self.entry_charges = entry_charges
        self.exit_value = Decimal(0)
        self.exit_qty = 0
        self.exit_charges = Decimal(0)
        self.exit_reason: str | None = None
        self.original_qty = plan.quantity


def _plan_to_dict(p: TradePlan) -> dict[str, Any]:
    d = asdict(p)
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, OptType):
            d[k] = v.value
    return d


def _plan_from_dict(d: dict[str, Any]) -> TradePlan:
    p = TradePlan(OptType(d["direction"]), Decimal(d["entry_price"]), int(d["quantity"]), int(d["lot_size"]),
                  Decimal(d["stop"]), Decimal(d["target"]), d["invalidation"], datetime.fromisoformat(d["opened_at"]))
    p.risk_per_unit, p.high_water = Decimal(d["risk_per_unit"]), Decimal(d["high_water"])
    p.partial_done, p.breakeven_set = bool(d["partial_done"]), bool(d["breakeven_set"])
    return p


class TradingEngine:
    def __init__(self, *, mode: TradingMode, risk_cfg: RiskConfig, strat: StrategyConfig, rates: ChargeRates,
                 data: BrokerBase, broker: BrokerBase, sessions: sessionmaker[Session], clock: Clock,
                 calendar: TradingCalendar, notifier: Notifier, health: DataHealth,
                 event_days: set[date] | None = None, order_manager: OrderManager | None = None) -> None:
        if mode is TradingMode.BACKTEST:
            raise EngineError("use the backtester for BACKTEST mode")
        self.mode, self.risk_cfg, self.strat, self.rates = mode, risk_cfg, strat, rates
        self.data, self.broker, self.sessions, self.clock = data, broker, sessions, clock
        self.calendar, self.notifier, self.health = calendar, notifier, health
        self.event_days = event_days or set()
        self.om = order_manager or OrderManager(broker, sessions, clock, mode.value)
        self.risk = RiskEngine(risk_cfg, rates, self._load_risk_state())
        self.lock = threading.RLock()
        self.manual_pause = False
        self.bars: list[Bar] = []
        self.agg: TickAggregator | None = None
        self.universe: OptionUniverse | None = None
        self.analysis_inst: Instrument | None = None
        self.index_inst: Instrument | None = None
        self.vix_inst: Instrument | None = None
        self.open: dict[int, OpenTrade] = {}
        self.last_signal_bar: datetime | None = None
        self.last_reconcile: datetime | None = None
        self.last_analysis: Analysis | None = None
        self.last_selection: Selection | None = None
        self.unreconciled = 0
        self.auth_ok = False

    # ------------------------------------------------------------------ setup
    def start(self, history_days: int = 6) -> None:
        sp = self.strat.signal
        try:
            self.data.authenticate()
            self.auth_ok = True
        except AuthError:
            self.auth_ok = False
            raise
        nfo = self.data.get_instruments(sp.options_exchange)
        nse = self.data.get_instruments(sp.underlying_exchange)
        self.universe = OptionUniverse.build(nfo, sp.underlying)
        today = self.clock.now().date()
        futs = sorted((i for i in nfo if i.name == sp.underlying and i.instrument_type == "FUT" and i.expiry
                       and i.expiry > today), key=lambda i: i.expiry)
        if not futs:
            raise EngineError(f"no {sp.underlying} future found for analysis data")
        self.analysis_inst = futs[0]
        index_symbol = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE"}.get(
            sp.underlying, sp.underlying)
        self.index_inst = next((i for i in nse if i.tradingsymbol == index_symbol), None)
        self.vix_inst = next((i for i in nse if i.tradingsymbol == "INDIA VIX"), None)
        if self.index_inst is None:
            raise EngineError(f"index {index_symbol} not found in instrument master")
        now = self.clock.now()
        self.bars = self.data.get_historical_data(self.analysis_inst.instrument_token,
                                                  now - timedelta(days=history_days + 4), now, "1m")
        self.bars = [b for b in self.bars if b.ts + timedelta(minutes=1) <= now]
        self.agg = TickAggregator(self.analysis_inst.instrument_token)
        self._restore_open_positions()
        self.reconcile(force=True)
        self._audit("system", "engine_started", {"mode": self.mode.value, "analysis": self.analysis_inst.tradingsymbol})
        self.notifier.notify("INFO", f"Engine started in {self.mode.value.upper()} mode")

    def tokens_to_stream(self) -> list[int]:
        toks = [i.instrument_token for i in (self.analysis_inst, self.index_inst, self.vix_inst) if i]
        toks += [t.inst.instrument_token for t in self.open.values()]
        return toks

    def on_tick(self, tick: Tick) -> None:
        if self.agg is None:
            return
        with self.lock:
            for b in self.agg.on_tick(tick):
                self.bars.append(b)

    # ------------------------------------------------------------------ persistence
    def _load_risk_state(self) -> RiskState:
        today = self.clock.now().date()
        with self.sessions() as s:
            row = s.get(RiskStateRow, self.mode.value)
            if row is not None:
                return RiskState.from_dict(row.state)
        from backend.risk.engine import iso_week
        return RiskState(day=today, week=iso_week(today))

    def _save_risk_state(self) -> None:
        with self.sessions.begin() as s:
            row = s.get(RiskStateRow, self.mode.value)
            if row is None:
                s.add(RiskStateRow(mode=self.mode.value, updated_at=self.clock.now(), state=self.risk.state.to_dict()))
            else:
                row.state, row.updated_at = self.risk.state.to_dict(), self.clock.now()

    def _risk_event(self, kind: str, message: str, severity: str = "warning", **details: Any) -> None:
        with self.sessions.begin() as s:
            s.add(RiskEvent(ts=self.clock.now(), mode=self.mode.value, kind=kind, severity=severity,
                            message=message, details={k: str(v) for k, v in details.items()}))

    def _audit(self, actor: str, action: str, details: dict[str, Any]) -> None:
        with self.sessions.begin() as s:
            s.add(AuditLog(ts=self.clock.now(), actor=actor, action=action, details=details))

    def _broker_event(self, kind: str, message: str) -> None:
        with self.sessions.begin() as s:
            s.add(BrokerEvent(ts=self.clock.now(), broker=self.broker.name, kind=kind, endpoint=None,
                              status_code=None, message=message))

    def _record_signal(self, a: Analysis, status: SignalStatus, score: int, reason: str,
                       sel: Selection | None) -> int:
        direction = {OptType.CE: "BUY_CE", OptType.PE: "BUY_PE"}.get(a.direction, "NO_TRADE") \
            if a.direction else "NO_TRADE"
        details: dict[str, Any] = {"components": a.components, "vetoes": a.vetoes, "notes": a.notes,
                                   "features": {k: (round(v, 4) if isinstance(v, float) else v)
                                                for k, v in a.features.items()},
                                   "invalidation": a.invalidation, "next_level": a.next_level}
        if sel is not None:
            details["selection"] = {"contract": sel.instrument.tradingsymbol if sel.instrument else None,
                                    "reasons": sel.reasons, "delta": sel.delta, "iv": sel.iv,
                                    "bid": str(sel.quote.best_bid) if sel.quote else None,
                                    "ask": str(sel.quote.best_ask) if sel.quote else None}
        with self.sessions.begin() as s:
            row = Signal(ts=a.ts, mode=self.mode.value, underlying=self.strat.signal.underlying, direction=direction,
                         score=max(0, min(100, score)), status=status.value, reason=reason, details=details)
            s.add(row)
            s.flush()
            return row.id

    def _restore_open_positions(self) -> None:
        with self.sessions() as s:
            rows = list(s.scalars(select(Position).where(Position.mode == self.mode.value,
                                                         Position.status != PositionStatus.CLOSED.value)))
            for p in rows:
                plan_row = s.get(PositionPlan, p.id)
                if plan_row is None or self.universe is None:
                    continue
                inst = next((c for c in self.universe.contracts if c.tradingsymbol == p.tradingsymbol), None)
                if inst is None:
                    continue
                plan = _plan_from_dict(plan_row.plan)
                plan.quantity = p.quantity
                self.open[p.id] = OpenTrade(p.id, inst, plan, Decimal(0))
                if hasattr(self.broker, "register_instrument"):
                    self.broker.register_instrument(inst)

    # ------------------------------------------------------------------ status
    def is_trading_day(self, d: date) -> bool | None:
        try:
            return self.calendar.is_trading_day(d)
        except CalendarUnavailable:
            return None

    def data_problems(self) -> list[str]:
        toks = [t for t in (self.analysis_inst, self.index_inst) if t]
        probs = self.health.problems([t.instrument_token for t in toks])
        if not self.auth_ok:
            probs.append("broker session not authenticated")
        if self.bars:
            age = self.clock.now() - (self.bars[-1].ts + timedelta(minutes=1))
            if self.calendar_open() and age > timedelta(minutes=3):
                probs.append("candles not updating")
        return probs

    def calendar_open(self) -> bool:
        try:
            return self.calendar.is_market_open(self.clock.now())
        except CalendarUnavailable:
            return False

    # ------------------------------------------------------------------ main loop
    def step(self) -> None:
        with self.lock:
            now = self.clock.now()
            self.risk.roll(now)
            if self.agg:
                self.bars.extend(self.agg.close_until(now))
            self.manage_positions()
            self.reconcile()
            self.maybe_enter()
            self._save_risk_state()

    def reconcile(self, force: bool = False) -> None:
        now = self.clock.now()
        if not force and self.last_reconcile and now - self.last_reconcile < timedelta(seconds=30):
            return
        self.last_reconcile = now
        if self.mode is TradingMode.SIGNAL_ONLY:
            return
        try:
            self.unreconciled = self.om.reconcile_unresolved()
            positions = self.broker.get_positions()
        except BrokerError as e:
            self._broker_event("RECONCILE_FAILED", str(e))
            return
        local = {t.inst.tradingsymbol: t.plan.quantity for t in self.open.values()}
        rep = reconcile(local, positions)
        for sym, (mine, theirs) in rep.adjusted.items():
            self._risk_event("POSITION_MISMATCH", f"{sym}: local {mine}, broker {theirs}; adopting broker")
            for t in list(self.open.values()):
                if t.inst.tradingsymbol == sym:
                    t.plan.quantity = theirs
                    if theirs == 0:
                        if t.exit_qty < t.original_qty:
                            # Closed outside the bot: the real exit price is unknown here. Book the
                            # missing quantity at the stop price (worst planned case) so the loss
                            # limits are never understated, and ask the user to check.
                            missing = t.original_qty - t.exit_qty
                            t.exit_value += t.plan.stop * missing
                            t.exit_qty += missing
                            t.exit_charges += leg_charges(self.rates, t.plan.stop, missing, False)
                            self._risk_event("EXIT_PRICE_UNKNOWN", f"{sym} closed outside the bot; booked "
                                             f"{missing} at stop {t.plan.stop} (conservative). Check broker.",
                                             "critical")
                        self._close_trade(t, "reconciled_flat")
        if rep.critical:
            self._risk_event("RECONCILE_CRITICAL", f"shorts={rep.shorts} unknown={rep.unknown_at_broker}",
                             severity="critical")
            self.notifier.notify("RISK", f"Reconciliation found unexpected positions: shorts={rep.shorts} "
                                         f"unknown={rep.unknown_at_broker}. Entries locked.", "critical")
            self.risk.emergency_stop()
        self.open = {k: v for k, v in self.open.items() if v.plan.quantity > 0}

    # ------------------------------------------------------------------ entries
    def maybe_enter(self) -> None:
        now = self.clock.now()
        if self.manual_pause or self.universe is None or not self.bars:
            return
        sig_min = self.strat.signal.signal_minutes
        sig_bars = resample(self.bars[-sig_min * 3:], sig_min, now=now)
        if not sig_bars:
            return
        latest = sig_bars[-1].ts
        if self.last_signal_bar is not None and latest <= self.last_signal_bar:
            return  # evaluate once per completed signal bar
        self.last_signal_bar = latest
        if now.date() != latest.date():
            return
        vix = self._ltp(self.vix_inst)
        a = analyse(self.bars, now, self.strat.signal, vix=vix, event_day=now.date() in self.event_days)
        self.last_analysis = a
        self.last_selection = None
        if a.direction is None:
            self._record_signal(a, SignalStatus.REJECTED, a.score_ex_liquidity,
                                "; ".join(a.vetoes + a.notes) or "no setup", None)
            return
        if a.vetoes:
            self._reject(a, a.score_ex_liquidity, "; ".join(a.vetoes), None)
            return
        spot = self._ltp(self.index_inst)
        if spot is None:
            self._reject(a, a.score_ex_liquidity, "no index spot price", None)
            return
        from backend.options.selection import candidate_strikes, pick_expiry
        expiry = pick_expiry(self.universe, now.date())
        if expiry is None:
            self._reject(a, a.score_ex_liquidity, "no expiry", None)
            return
        strikes = candidate_strikes(self.universe, expiry, spot, a.direction, self.strat.contract.max_itm_steps)
        cands = [self.universe.contract(expiry, k, a.direction) for k in strikes]
        try:
            quotes = self.data.get_quote(cands)
        except BrokerError as e:
            self._reject(a, a.score_ex_liquidity, f"quote fetch failed: {e}", None)
            return
        sel = select_contract(self.universe, a.direction, spot, now, quotes, self.strat.contract)
        self.last_selection = sel
        score = a.total_score(sel.liquidity_points)
        if not sel.ok:
            self._reject(a, score, "no acceptable contract: " + " | ".join(sel.reasons), sel)
            return
        assert sel.instrument is not None and sel.quote is not None and sel.quote.best_ask is not None
        inst, q = sel.instrument, sel.quote
        if score < self.risk_cfg.min_signal_score_for_entry or self.mode is TradingMode.SIGNAL_ONLY:
            status_reason = ("signal only mode" if self.mode is TradingMode.SIGNAL_ONLY
                             else f"score {score} below threshold {self.risk_cfg.min_signal_score_for_entry}")
            sid = self._record_signal(a, SignalStatus.REJECTED if self.mode is not TradingMode.SIGNAL_ONLY
                                      else SignalStatus.GENERATED, score, status_reason, sel)
            self.notifier.notify("SIGNAL", f"{a.direction.value} {inst.tradingsymbol} score {score}: {status_reason}"
                                 f" (signal #{sid})")
            return

        slip = Decimal(str(self.risk_cfg.orders.entry_slippage_pct))
        limit = round_to_tick(q.best_ask * (1 + slip), inst.tick_size, up=True)
        under_stop = None
        if sel.delta and a.invalidation is not None:
            move = Decimal(str(abs(spot - a.invalidation) * sel.delta))
            under_stop = q.best_ask - move
        plan = TradePlan.build(a.direction, limit, 1, inst.lot_size, self.strat.exits, now, a.invalidation, under_stop)
        try:
            funds = self.broker.get_balance().available_cash
        except BrokerError as e:
            self._reject(a, score, f"balance unavailable: {e}", sel)
            return
        ctx = EntryContext(
            now=now, direction=a.direction, score=score, entry_price=limit, stop_price=plan.stop,
            exit_slippage=(plan.stop * Decimal(str(self.risk_cfg.orders.exit_slippage_pct))),
            lot_size=inst.lot_size, available_funds=funds, open_positions=len(self.open), pending_entries=0,
            has_position_in_contract=any(t.inst.tradingsymbol == inst.tradingsymbol for t in self.open.values()),
            data_problems=tuple(self.data_problems()), is_trading_day=bool(self.is_trading_day(now.date())),
            is_expiry_day=now.date() in set(self.universe.expiries()), unreconciled_orders=self.unreconciled)
        decision = self.risk.check_entry(ctx)
        if not decision.approved:
            self._reject(a, score, "risk: " + "; ".join(decision.reasons), sel)
            return
        sid = self._record_signal(a, SignalStatus.ACCEPTED, score, f"{decision.lots} lot(s), max loss "
                                  f"Rs {decision.max_loss}", sel)
        self.notifier.notify("SIGNAL", f"BUY {a.direction.value} {inst.tradingsymbol} score {score}, limit {limit}, "
                                       f"stop {plan.stop}, target {plan.target}, qty {decision.quantity}")
        if hasattr(self.broker, "register_instrument"):
            self.broker.register_instrument(inst)
        self.notifier.notify("ORDER_PLACED", f"Entry order {inst.tradingsymbol} x{decision.quantity} @ {limit}")
        out = self.om.submit_entry(inst, limit, decision, self.risk_cfg.orders.entry_timeout_seconds, signal_id=sid)
        if out.status is DbOrderStatus.UNKNOWN:
            self.unreconciled += 1
            self._risk_event("ORDER_UNKNOWN", f"entry {out.client_order_id} outcome unknown", "critical")
            self.notifier.notify("RISK", "Entry order outcome unknown; entries blocked until reconciled", "critical")
        if out.filled_quantity <= 0:
            self.notifier.notify("TRADE_REJECTED", f"Entry not filled ({out.status.value}): {out.message or ''}")
            return
        fill = out.average_price or limit
        plan = TradePlan.build(a.direction, fill, out.filled_quantity, inst.lot_size, self.strat.exits, now,
                               a.invalidation, under_stop - (limit - fill) if under_stop is not None else None)
        self.risk.on_entry_filled(now)
        entry_charges = leg_charges(self.rates, fill, out.filled_quantity, True)
        with self.sessions.begin() as s:
            pos = Position(mode=self.mode.value, exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
                           instrument_token=inst.instrument_token, underlying=inst.name,
                           option_type=inst.instrument_type, strike=inst.strike, expiry=inst.expiry,
                           lot_size=inst.lot_size, quantity=out.filled_quantity, avg_entry_price=fill,
                           stop_loss=plan.stop, target=plan.target, status=PositionStatus.OPEN.value, opened_at=now)
            s.add(pos)
            s.flush()
            s.add(PositionPlan(position_id=pos.id, plan=_plan_to_dict(plan)))
            pid = pos.id
        from backend.database.models import Order
        with self.sessions.begin() as s:
            o = s.scalars(select(Order).where(Order.client_order_id == out.client_order_id)).one()
            o.position_id = pid
        self.open[pid] = OpenTrade(pid, inst, plan, entry_charges)
        self.notifier.notify("ORDER_EXECUTED", f"Bought {out.filled_quantity} {inst.tradingsymbol} @ {fill}; "
                                               f"stop {plan.stop}, target {plan.target}")

    def _reject(self, a: Analysis, score: int, reason: str, sel: Selection | None) -> None:
        sid = self._record_signal(a, SignalStatus.REJECTED, score, reason, sel)
        self.notifier.notify("TRADE_REJECTED", f"{a.direction.value if a.direction else ''} signal #{sid} "
                                               f"rejected: {reason}")

    def _ltp(self, inst: Instrument | None) -> float | None:
        if inst is None:
            return None
        try:
            q = self.data.get_quote([inst]).get(inst.instrument_token)
        except BrokerError:
            return None
        if q is None or self.clock.now() - q.ts > timedelta(seconds=10):
            return None
        return float(q.last_price)

    # ------------------------------------------------------------------ exits
    def manage_positions(self, emergency: bool = False) -> None:
        if not self.open or self.mode is TradingMode.SIGNAL_ONLY:
            return
        now = self.clock.now()
        insts = [t.inst for t in self.open.values()]
        try:
            quotes = self.data.get_quote(insts)
        except BrokerError:
            quotes = {}
        spot = self._ltp(self.index_inst)
        sig = resample(self.bars[-self.strat.signal.signal_minutes * 3:], self.strat.signal.signal_minutes, now=now)
        vw = vwap([b for b in self.bars if b.ts.date() == now.date()])
        mtm = Decimal(0)
        for t in list(self.open.values()):
            q = quotes.get(t.inst.instrument_token)
            bid = q.best_bid if q is not None and now - q.ts <= timedelta(seconds=10) else None
            data_ok = bid is not None and not self.data_problems()
            mv = MarketView(now=now, option_bid=bid, spot=spot,
                            last_signal_close=sig[-1].close if sig and sig[-1].ts.date() == now.date() else None,
                            vwap=vw[-1] if vw else None, data_ok=data_ok)
            locked = bool(self.risk.state.day_lock_reason or self.risk.state.week_lock_reason)
            decision = evaluate(t.plan, mv, self.strat.exits, self.risk_cfg.session.square_off,
                                emergency=emergency or self.risk.state.emergency_lock, risk_lock=locked,
                                emergency_policy=self.risk_cfg.emergency_exit_policy)
            self._save_plan(t)
            if decision is not None:
                self.execute_exit(t, decision, bid)
            elif bid is not None:
                mtm += (bid - t.plan.entry_price) * t.plan.quantity
        if self.risk.update_mtm(mtm, now):
            self._risk_event("DAILY_LOSS_LIMIT", "loss limit reached including open MTM", "critical")
            self.notifier.notify("DAILY_LOSS_LIMIT", "Daily loss limit reached: entries stopped", "critical")

    def _save_plan(self, t: OpenTrade) -> None:
        with self.sessions.begin() as s:
            row = s.get(PositionPlan, t.position_id)
            if row is not None:
                row.plan = _plan_to_dict(t.plan)
            pos = s.get(Position, t.position_id)
            if pos is not None:
                pos.stop_loss, pos.target = t.plan.stop, t.plan.target

    def verified_qty(self, symbol: str) -> int:
        for p in self.broker.get_positions():
            if p.tradingsymbol == symbol and p.product == "MIS":
                return max(p.quantity, 0)
        return 0

    def execute_exit(self, t: OpenTrade, d: ExitDecision, bid: Decimal | None) -> None:
        """Exit with a limit ladder; escalate urgent exits to protected MARKET orders."""
        remaining = min(d.quantity, t.plan.quantity)
        attempts = 0
        slip = Decimal(str(self.risk_cfg.orders.exit_slippage_pct))
        while remaining > 0 and attempts < 4:
            attempts += 1
            try:
                held = self.verified_qty(t.inst.tradingsymbol)
            except BrokerError as e:
                self._broker_event("POSITION_CHECK_FAILED", str(e))
                return
            if held <= 0:
                break
            use_market = d.urgent and (bid is None or attempts >= 3)
            limit = None if use_market else round_to_tick((bid or Decimal("0.05")) * (1 - slip * attempts),
                                                          t.inst.tick_size, up=False)
            out = self.om.submit_exit(t.inst, remaining, held, limit, self.risk_cfg.orders.exit_timeout_seconds,
                                      position_id=t.position_id)
            if out.filled_quantity > 0:
                px = out.average_price or limit or Decimal(0)
                t.exit_value += px * out.filled_quantity
                t.exit_qty += out.filled_quantity
                t.exit_charges += leg_charges(self.rates, px, out.filled_quantity, False)
                t.plan.quantity -= out.filled_quantity
                remaining -= out.filled_quantity
            if out.status is DbOrderStatus.UNKNOWN:
                self.unreconciled += 1
                break
            bid = self._fresh_bid(t.inst)
        event = {"stop_loss": "STOP_LOSS", "trailing_stop": "STOP_LOSS", "target": "TARGET",
                 "partial_target": "TARGET"}.get(d.reason, "TRADE_EXITED")
        if remaining > 0:
            self._risk_event("EXIT_INCOMPLETE", f"{t.inst.tradingsymbol}: {remaining} not exited ({d.reason})",
                             "critical")
            self.notifier.notify("RISK", f"Exit incomplete for {t.inst.tradingsymbol}: {remaining} left. "
                                         "Check broker immediately.", "critical")
        else:
            self.notifier.notify(event, f"Exited {d.quantity} {t.inst.tradingsymbol} ({d.reason})")
        t.exit_reason = d.reason
        with self.sessions.begin() as s:
            pos = s.get(Position, t.position_id)
            if pos is not None:
                pos.quantity = t.plan.quantity
                pos.status = PositionStatus.CLOSED.value if t.plan.quantity == 0 else PositionStatus.OPEN.value
        if t.plan.quantity == 0:
            self._close_trade(t, d.reason)

    def _fresh_bid(self, inst: Instrument) -> Decimal | None:
        try:
            q = self.data.get_quote([inst]).get(inst.instrument_token)
        except BrokerError:
            return None
        return q.best_bid if q else None

    def _close_trade(self, t: OpenTrade, reason: str) -> None:
        now = self.clock.now()
        qty = t.exit_qty or t.original_qty
        avg_exit = (t.exit_value / t.exit_qty) if t.exit_qty else Decimal(0)
        gross = (avg_exit - t.plan.entry_price) * t.exit_qty
        charges = t.entry_charges + t.exit_charges
        net = gross - charges
        with self.sessions.begin() as s:
            pos = s.get(Position, t.position_id)
            if pos is not None:
                pos.status, pos.closed_at, pos.quantity = PositionStatus.CLOSED.value, now, 0
                if s.scalars(select(Trade).where(Trade.position_id == t.position_id)).first() is None:
                    s.add(Trade(position_id=t.position_id, mode=self.mode.value, entry_time=pos.opened_at,
                                exit_time=now, quantity=qty, avg_entry_price=t.plan.entry_price,
                                avg_exit_price=avg_exit, gross_pnl=gross, charges=charges, net_pnl=net,
                                exit_reason=reason))
        self.open.pop(t.position_id, None)
        for ev in self.risk.on_trade_closed(net, t.plan.direction, reason, now):
            msg = {"PAUSED": "Paused after consecutive losses", "STOPPED_FOR_DAY": "Stopped for the day",
                   "LOSS_LIMIT": "Loss limit reached"}[ev]
            self._risk_event(ev, msg, "warning" if ev == "PAUSED" else "critical")
            self.notifier.notify("BOT_PAUSED" if ev == "PAUSED" else "DAILY_LOSS_LIMIT", msg,
                                 "warning" if ev == "PAUSED" else "critical")
        self.notifier.notify("TRADE_EXITED", f"Closed {t.inst.tradingsymbol}: net P&L Rs {net:.2f} ({reason})")

    # ------------------------------------------------------------------ controls
    def pause(self, actor: str) -> None:
        with self.lock:
            self.manual_pause = True
            self._audit(actor, "pause", {})
            self.notifier.notify("BOT_PAUSED", f"Paused by {actor}", "warning")

    def resume(self, actor: str) -> None:
        with self.lock:
            if self.risk.state.emergency_lock:
                raise EngineError("emergency lock active: use reset with confirmation")
            self.manual_pause = False
            self._audit(actor, "resume", {})

    def emergency_stop(self, actor: str) -> dict[str, Any]:
        """Works without AI or network to anything but the broker. Idempotent."""
        with self.lock:
            self.risk.emergency_stop()
            self.manual_pause = True
            self._risk_event("EMERGENCY_STOP", f"by {actor}", "critical")
            self._audit(actor, "emergency_stop", {})
            self.notifier.notify("EMERGENCY_STOP", f"Emergency stop by {actor}", "critical")
            cancelled = self.om.cancel_open_entries()
            if self.risk_cfg.emergency_exit_policy.value == "exit_all":
                self.manage_positions(emergency=True)
            self.reconcile(force=True)
            self._save_risk_state()
            return {"cancelled_entries": cancelled, "open_positions": len(self.open)}

    def close_all(self, actor: str) -> int:
        with self.lock:
            self._audit(actor, "close_all", {})
            n = len(self.open)
            for t in list(self.open.values()):
                self.execute_exit(t, ExitDecision("manual_close_all", t.plan.quantity, urgent=True),
                                  self._fresh_bid(t.inst))
            self.reconcile(force=True)
            return n

    def cancel_all(self, actor: str) -> int:
        with self.lock:
            self._audit(actor, "cancel_all", {})
            return self.om.cancel_open_entries()

    def reset_emergency(self, actor: str) -> None:
        with self.lock:
            self.risk.manual_reset(clear_emergency=True)
            self._audit(actor, "reset_emergency", {})
            self._save_risk_state()
            self.notifier.notify("INFO", f"Emergency lock cleared by {actor}; trading stays paused until resumed")

    # ------------------------------------------------------------------ views
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = self.clock.now()
            s = self.risk.state
            c = self.risk_cfg
            a, sel = self.last_analysis, self.last_selection
            open_rows = []
            quotes = {}
            if self.open:
                try:
                    quotes = self.data.get_quote([t.inst for t in self.open.values()])
                except BrokerError:
                    quotes = {}
            exposure = Decimal(0)
            for t in self.open.values():
                q = quotes.get(t.inst.instrument_token)
                cur = q.best_bid if q else None
                exposure += t.plan.entry_price * t.plan.quantity
                open_rows.append({
                    "position_id": t.position_id, "symbol": t.inst.tradingsymbol, "type": t.inst.instrument_type,
                    "strike": str(t.inst.strike), "expiry": str(t.inst.expiry), "entry": str(t.plan.entry_price),
                    "current": str(cur) if cur is not None else None, "quantity": t.plan.quantity,
                    "unrealised": str((cur - t.plan.entry_price) * t.plan.quantity) if cur is not None else None,
                    "stop": str(t.plan.stop), "target": str(t.plan.target),
                    "minutes_in_trade": int((now - t.plan.opened_at).total_seconds() // 60)})
            remaining = Decimal(c.capital.max_daily_loss) + min(s.realised_pnl_today + s.open_mtm, Decimal(0))
            try:
                funds = str(self.broker.get_balance().available_cash)
            except BrokerError:
                funds = None
            return {
                "now": now.isoformat(), "mode": self.mode.value, "live_enabled": self.mode is TradingMode.LIVE,
                "paused": self.manual_pause, "emergency_lock": s.emergency_lock,
                "day_lock": s.day_lock_reason, "week_lock": s.week_lock_reason,
                "paused_until": s.paused_until.isoformat() if s.paused_until else None,
                "data_problems": self.data_problems(), "market_open": self.calendar_open(),
                "trading_day": self.is_trading_day(now.date()),
                "analysis_instrument": self.analysis_inst.tradingsymbol if self.analysis_inst else None,
                "analysis": None if a is None else {
                    "ts": a.ts.isoformat(), "direction": a.direction.value if a.direction else "NO_TRADE",
                    "score_ex_liquidity": a.score_ex_liquidity, "components": a.components, "weights": WEIGHTS,
                    "vetoes": a.vetoes, "notes": a.notes,
                    "features": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in a.features.items()},
                    "invalidation": a.invalidation},
                "selection": None if sel is None else {
                    "contract": sel.instrument.tradingsymbol if sel.instrument else None,
                    "expiry": str(sel.instrument.expiry) if sel.instrument else None,
                    "strike": str(sel.instrument.strike) if sel.instrument else None,
                    "bid": str(sel.quote.best_bid) if sel.quote else None,
                    "ask": str(sel.quote.best_ask) if sel.quote else None,
                    "lot_size": sel.instrument.lot_size if sel.instrument else None,
                    "delta": sel.delta, "liquidity_points": sel.liquidity_points, "reasons": sel.reasons},
                "spot": self._ltp(self.index_inst), "vix": self._ltp(self.vix_inst),
                "positions": open_rows,
                "risk": {"capital": c.capital.total_capital, "available_funds": funds,
                         "deployed": str(exposure), "max_daily_loss": c.capital.max_daily_loss,
                         "realised_today": str(s.realised_pnl_today), "open_mtm": str(s.open_mtm),
                         "remaining_daily_risk": str(max(remaining, Decimal(0))),
                         "trades_today": s.trades_today,
                         "trades_remaining": max(c.trades.max_trades_per_day - s.trades_today, 0),
                         "consecutive_losses": s.consecutive_losses,
                         "max_loss_per_trade": c.capital.max_loss_per_trade},
                "alerts": [{"ts": al.ts.isoformat(), "event": al.event, "severity": al.severity,
                            "message": al.message} for al in list(self.notifier.alerts)[:50]],
            }
