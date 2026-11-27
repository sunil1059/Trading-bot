"""Paper broker: virtual orders and capital, filled against LIVE quotes.

Market data always comes from the real data broker (never synthesized). Fills are
conservative: BUY fills at the best ask (only if ask <= limit), SELL at the best bid,
limited by the displayed top-of-book quantity, so partial fills happen naturally.
Stale quotes never fill. Orders are evaluated when their status is polled.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from backend.brokers.base import BrokerBase, DataError, InputError, OrderRejected
from backend.core.clock import Clock
from backend.models.domain import (
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerTrade,
    Funds,
    Instrument,
    OrderRequest,
    Profile,
    Quote,
    Side,
)
from backend.risk.charges import ChargeRates, leg_charges


@dataclass
class _PaperOrder:
    req: OrderRequest
    order: BrokerOrder
    created: datetime
    fill_value: Decimal = Decimal(0)


class PaperBroker(BrokerBase):
    name = "paper"

    def __init__(self, data_broker: BrokerBase, clock: Clock, starting_cash: Decimal, rates: ChargeRates,
                 quote_fn: Callable[[Instrument], Quote | None] | None = None,
                 max_quote_age: timedelta = timedelta(seconds=5), market_protection_pct: Decimal = Decimal("0.05"),
                 fill_latency: timedelta = timedelta(seconds=0)) -> None:
        self.data, self.clock, self.rates = data_broker, clock, rates
        self.cash = starting_cash
        self.quote_fn = quote_fn or (lambda inst: self.data.get_quote([inst]).get(inst.instrument_token))
        self.max_quote_age, self.protection, self.latency = max_quote_age, market_protection_pct, fill_latency
        self._orders: dict[str, _PaperOrder] = {}
        self._insts: dict[str, Instrument] = {}
        self._pos: dict[str, list] = {}  # symbol -> [qty, avg_price, Instrument]
        self._trades: list[BrokerTrade] = []
        self._seq = 0
        self._lock = threading.RLock()
        self.charges_paid = Decimal(0)

    # --- delegated market data (always real) ---
    def authenticate(self) -> None:
        self.data.authenticate()

    def get_profile(self) -> Profile:
        p = self.data.get_profile()
        return Profile(p.user_id, p.user_name, "paper")

    def get_instruments(self, exchange):
        return self.data.get_instruments(exchange)

    def get_quote(self, instruments):
        return self.data.get_quote(instruments)

    def get_historical_data(self, *a, **k):
        return self.data.get_historical_data(*a, **k)

    def register_instrument(self, inst: Instrument) -> None:
        self._insts[inst.tradingsymbol] = inst

    # --- virtual account ---
    def get_balance(self) -> Funds:
        with self._lock:
            return Funds(available_cash=self.cash)

    def place_order(self, req: OrderRequest) -> str:
        with self._lock:
            if req.order_type not in ("LIMIT", "MARKET"):
                raise InputError("paper broker supports LIMIT and MARKET only")
            if req.side is Side.BUY and req.order_type != "LIMIT":
                raise InputError("entries must be LIMIT")
            if any(po.req.client_order_id == req.client_order_id for po in self._orders.values()):
                raise InputError("duplicate client order id")
            if req.side is Side.SELL:
                held = self._pos.get(req.tradingsymbol, [0])[0]
                pending = sum(po.order.pending_quantity for po in self._orders.values()
                              if po.req.side is Side.SELL and po.req.tradingsymbol == req.tradingsymbol
                              and not po.order.status.is_terminal)
                if req.quantity > held - pending:
                    raise OrderRejected("sell quantity exceeds long position (shorting not allowed)")
            else:
                assert req.limit_price is not None
                if req.limit_price * req.quantity > self.cash:
                    raise OrderRejected("insufficient virtual funds")
            self._seq += 1
            oid = f"P{self._seq:08d}"
            bo = BrokerOrder(oid, req.client_order_id, req.tradingsymbol, req.side, req.quantity, 0, None,
                             BrokerOrderStatus.OPEN, updated_at=self.clock.now())
            self._orders[oid] = _PaperOrder(req, bo, self.clock.now())
            return oid

    def modify_order(self, broker_order_id, *, limit_price=None, quantity=None, trigger_price=None) -> None:
        raise InputError("modify not supported in paper mode; cancel and re-place")

    def cancel_order(self, broker_order_id: str) -> None:
        with self._lock:
            po = self._orders.get(broker_order_id)
            if po is None:
                raise InputError("unknown order")
            if not po.order.status.is_terminal:
                po.order.status = BrokerOrderStatus.CANCELLED
                po.order.updated_at = self.clock.now()

    def _inst(self, symbol: str, token: int) -> Instrument:
        inst = self._insts.get(symbol)
        if inst is None:
            raise DataError(f"instrument {symbol} not registered with paper broker")
        return inst

    def _try_fill(self, po: _PaperOrder) -> None:
        o = po.order
        if o.status.is_terminal or self.clock.now() - po.created < self.latency:
            return
        q = self.quote_fn(self._inst(po.req.tradingsymbol, po.req.instrument_token))
        if q is None or self.clock.now() - q.ts > self.max_quote_age:
            return  # stale or missing data never fills
        if po.req.side is Side.BUY:
            if not q.asks or q.best_ask is None or q.best_ask > po.req.limit_price:
                return
            price, avail = q.best_ask, q.asks[0].quantity
        else:
            if not q.bids or q.best_bid is None:
                return
            floor = po.req.limit_price if po.req.limit_price is not None else q.best_bid * (1 - self.protection)
            if q.best_bid < floor:
                return
            price, avail = q.best_bid, q.bids[0].quantity
        qty = min(o.pending_quantity, avail)
        if qty <= 0:
            return
        self._book_fill(po, qty, price)

    def _book_fill(self, po: _PaperOrder, qty: int, price: Decimal) -> None:
        o, sym = po.order, po.req.tradingsymbol
        first = o.filled_quantity == 0  # brokerage is charged once per executed order
        charges = leg_charges(self.rates, price, qty, po.req.side is Side.BUY, orders=1 if first else 0)
        self.charges_paid += charges
        pos = self._pos.setdefault(sym, [0, Decimal(0), po.req.instrument_token])
        if po.req.side is Side.BUY:
            self.cash -= price * qty + charges
            new_qty = pos[0] + qty
            pos[1] = (pos[1] * pos[0] + price * qty) / new_qty
            pos[0] = new_qty
        else:
            self.cash += price * qty - charges
            pos[0] -= qty
            if pos[0] == 0:
                pos[1] = Decimal(0)
        po.fill_value += price * qty
        o.filled_quantity += qty
        o.average_price = (po.fill_value / o.filled_quantity).quantize(Decimal("0.0001"))
        o.status = BrokerOrderStatus.FILLED if o.pending_quantity == 0 else BrokerOrderStatus.PARTIALLY_FILLED
        o.updated_at = self.clock.now()
        self._trades.append(BrokerTrade(f"T{len(self._trades) + 1:08d}", o.broker_order_id, qty, price,
                                        self.clock.now()))

    def get_order_book(self) -> list[BrokerOrder]:
        with self._lock:
            for po in self._orders.values():
                self._try_fill(po)
            return [po.order for po in self._orders.values()]

    def get_order_status(self, broker_order_id: str) -> BrokerOrder:
        with self._lock:
            po = self._orders.get(broker_order_id)
            if po is None:
                raise DataError("unknown order")
            self._try_fill(po)
            return po.order

    def get_trade_history(self) -> list[BrokerTrade]:
        with self._lock:
            return list(self._trades)

    def get_positions(self) -> list[BrokerPosition]:
        with self._lock:
            return [BrokerPosition("NFO", sym, int(p[2]), p[0], p[1], "MIS") for sym, p in self._pos.items()]
