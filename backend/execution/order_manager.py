"""Idempotent order placement and tracking.

Guarantees:
- An order row (PENDING_SUBMIT) with a unique client id is committed BEFORE the broker
  call, so a crash or timeout can always be reconciled by that id.
- A timed-out placement is never blindly re-sent: we look the client id up in the broker
  order book first. If it cannot be found, the order stays UNKNOWN and new entries are
  blocked until reconciliation resolves it.
- Acceptance is not execution: fills come only from broker order status.
- Entry orders require an approved RiskDecision of the same quantity.
- Exit orders are SELL and never exceed the broker-verified long quantity.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.brokers.base import AuthError, BrokerBase, BrokerError, NetworkError, OrderRejected
from backend.core.clock import Clock
from backend.database.models import Order, OrderPurpose, OrderStatus
from backend.models.domain import BrokerOrder, BrokerOrderStatus, Instrument, OrderRequest, Side
from backend.risk.engine import RiskDecision

log = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    pass


def new_client_id(prefix: str) -> str:
    return (prefix + secrets.token_hex(8)).upper()[:16]


@dataclass(frozen=True)
class OrderOutcome:
    client_order_id: str
    broker_order_id: str | None
    status: OrderStatus
    filled_quantity: int
    average_price: Decimal | None
    message: str | None = None


_MAP = {
    BrokerOrderStatus.OPEN: OrderStatus.OPEN,
    BrokerOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    BrokerOrderStatus.FILLED: OrderStatus.FILLED,
    BrokerOrderStatus.CANCELLED: OrderStatus.CANCELLED,
    BrokerOrderStatus.REJECTED: OrderStatus.REJECTED,
    BrokerOrderStatus.UNKNOWN: OrderStatus.UNKNOWN,
}


class OrderManager:
    def __init__(self, broker: BrokerBase, sessions: sessionmaker[Session], clock: Clock, mode: str,
                 sleep: Callable[[float], None] = time.sleep, poll_interval: float = 0.5,
                 lookup_attempts: int = 4, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.broker, self.sessions, self.clock, self.mode = broker, sessions, clock, mode
        self.monotonic = monotonic
        self.sleep, self.poll_interval, self.lookup_attempts = sleep, poll_interval, lookup_attempts

    # --- public API -----------------------------------------------------------------
    def submit_entry(self, inst: Instrument, limit_price: Decimal, decision: RiskDecision, timeout_s: float,
                     signal_id: int | None = None) -> OrderOutcome:
        if not decision.approved or decision.quantity <= 0:
            raise ExecutionError("entry without an approved risk decision")
        if decision.quantity % inst.lot_size:
            raise ExecutionError("quantity is not a whole number of lots")
        req = OrderRequest(new_client_id("E"), inst.exchange, inst.tradingsymbol, inst.instrument_token, Side.BUY,
                           decision.quantity, "LIMIT", limit_price)
        return self._run(req, OrderPurpose.ENTRY, timeout_s, signal_id=signal_id)

    def submit_exit(self, inst: Instrument, quantity: int, verified_long_qty: int, limit_price: Decimal | None,
                    timeout_s: float, position_id: int | None = None) -> OrderOutcome:
        if verified_long_qty <= 0:
            raise ExecutionError("no verified long position to exit")
        qty = min(quantity, verified_long_qty)  # never sell more than we hold: no reverse position
        if qty <= 0:
            raise ExecutionError("exit quantity must be positive")
        order_type = "LIMIT" if limit_price is not None else "MARKET"
        req = OrderRequest(new_client_id("X"), inst.exchange, inst.tradingsymbol, inst.instrument_token, Side.SELL,
                           qty, order_type, limit_price)
        return self._run(req, OrderPurpose.EXIT, timeout_s, position_id=position_id)

    def unresolved_orders(self) -> list[Order]:
        with self.sessions() as s:
            return list(s.scalars(select(Order).where(
                Order.mode == self.mode,
                Order.status.in_([OrderStatus.PENDING_SUBMIT.value, OrderStatus.UNKNOWN.value]))))

    def reconcile_unresolved(self) -> int:
        """Resolve PENDING_SUBMIT/UNKNOWN orders against the broker. Returns how many remain."""
        remaining = 0
        for o in self.unresolved_orders():
            found = self.broker.find_order_by_client_id(o.client_order_id)
            if found is None:
                remaining += 1
                continue
            self._apply(o.client_order_id, found)
        return remaining

    def cancel_open_entries(self) -> int:
        n = 0
        with self.sessions() as s:
            rows = list(s.scalars(select(Order).where(
                Order.mode == self.mode, Order.purpose == OrderPurpose.ENTRY.value,
                Order.status.in_([OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value]))))
        for o in rows:
            if o.broker_order_id:
                try:
                    self.broker.cancel_order(o.broker_order_id)
                    n += 1
                except BrokerError:
                    log.exception("cancel failed", extra={"client_order_id": o.client_order_id})
        return n

    # --- internals ------------------------------------------------------------------
    def _persist_new(self, req: OrderRequest, purpose: OrderPurpose, signal_id: int | None,
                     position_id: int | None) -> None:
        now = self.clock.now()
        with self.sessions.begin() as s:
            s.add(Order(client_order_id=req.client_order_id, mode=self.mode, exchange=req.exchange,
                        tradingsymbol=req.tradingsymbol, instrument_token=req.instrument_token, side=req.side.value,
                        purpose=purpose.value, order_type=req.order_type, quantity=req.quantity,
                        limit_price=req.limit_price, trigger_price=req.trigger_price,
                        status=OrderStatus.PENDING_SUBMIT.value, filled_quantity=0, created_at=now,
                        updated_at=now, signal_id=signal_id, position_id=position_id))

    def _update(self, client_id: str, **fields) -> None:
        with self.sessions.begin() as s:
            o = s.scalars(select(Order).where(Order.client_order_id == client_id)).one()
            for k, v in fields.items():
                setattr(o, k, v)
            o.updated_at = self.clock.now()

    def _apply(self, client_id: str, bo: BrokerOrder) -> OrderOutcome:
        status = _MAP[bo.status]
        self._update(client_id, broker_order_id=bo.broker_order_id, status=status.value,
                     filled_quantity=bo.filled_quantity, average_price=bo.average_price,
                     status_message=bo.status_message)
        return OrderOutcome(client_id, bo.broker_order_id, status, bo.filled_quantity, bo.average_price,
                            bo.status_message)

    def _lookup(self, client_id: str) -> BrokerOrder | None:
        for i in range(self.lookup_attempts):
            try:
                found = self.broker.find_order_by_client_id(client_id)
                if found is not None:
                    return found
            except BrokerError:
                log.warning("order lookup failed", extra={"client_order_id": client_id, "attempt": i + 1})
            self.sleep(self.poll_interval * (i + 1))
        return None

    def _run(self, req: OrderRequest, purpose: OrderPurpose, timeout_s: float, signal_id: int | None = None,
             position_id: int | None = None) -> OrderOutcome:
        self._persist_new(req, purpose, signal_id, position_id)
        cid = req.client_order_id
        try:
            broker_id = self.broker.place_order(req)
            self._update(cid, broker_order_id=broker_id, status=OrderStatus.OPEN.value)
        except OrderRejected as e:
            self._update(cid, status=OrderStatus.REJECTED.value, status_message=str(e))
            return OrderOutcome(cid, None, OrderStatus.REJECTED, 0, None, str(e))
        except (NetworkError, AuthError, BrokerError) as e:
            # Outcome unknown: the broker may or may not have the order. Look it up by tag.
            self._update(cid, status=OrderStatus.UNKNOWN.value, status_message=f"placement error: {e}")
            found = self._lookup(cid)
            if found is None:
                log.error("order outcome unknown", extra={"client_order_id": cid})
                return OrderOutcome(cid, None, OrderStatus.UNKNOWN, 0, None, str(e))
            broker_id = found.broker_order_id
            self._apply(cid, found)
        return self._track(cid, broker_id, timeout_s)

    def _track(self, cid: str, broker_id: str, timeout_s: float) -> OrderOutcome:
        deadline = self.monotonic() + timeout_s
        last: BrokerOrder | None = None
        cancel_sent = False
        while True:
            try:
                last = self.broker.get_order_status(broker_id)
            except BrokerError:
                log.warning("status poll failed", extra={"client_order_id": cid})
                last = None
            if last is not None:
                outcome = self._apply(cid, last)
                if last.status.is_terminal:
                    return outcome
            if self.monotonic() >= deadline and not cancel_sent:
                try:
                    self.broker.cancel_order(broker_id)
                except BrokerError as e:
                    log.warning("cancel failed; will keep polling", extra={"client_order_id": cid, "err": str(e)})
                cancel_sent = True
                deadline = self.monotonic() + max(timeout_s, 5)
            elif self.monotonic() >= deadline and cancel_sent:
                # Still not terminal after cancel: report what we know; reconciliation continues.
                if last is not None:
                    return self._apply(cid, last)
                self._update(cid, status=OrderStatus.UNKNOWN.value)
                return OrderOutcome(cid, broker_id, OrderStatus.UNKNOWN, 0, None, "status unavailable")
            self.sleep(self.poll_interval)
