"""Broker-neutral domain types shared by every layer.

Prices are Decimal at the order/accounting boundary and float inside indicator maths.
All datetimes are timezone-aware (Asia/Kolkata).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OptType(StrEnum):
    CE = "CE"
    PE = "PE"


class BrokerOrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (BrokerOrderStatus.FILLED, BrokerOrderStatus.CANCELLED, BrokerOrderStatus.REJECTED)


@dataclass(frozen=True)
class Instrument:
    instrument_token: int
    exchange: str
    tradingsymbol: str
    name: str  # underlying name, e.g. NIFTY
    instrument_type: str  # CE / PE / FUT / EQ / INDEX
    segment: str
    lot_size: int
    tick_size: Decimal
    expiry: date | None = None
    strike: Decimal | None = None

    @property
    def is_option(self) -> bool:
        return self.instrument_type in ("CE", "PE")


@dataclass(frozen=True)
class DepthLevel:
    price: Decimal
    quantity: int
    orders: int = 0


@dataclass(frozen=True)
class Quote:
    instrument_token: int
    last_price: Decimal
    ts: datetime  # exchange timestamp of the quote
    last_trade_time: datetime | None = None
    volume: int = 0
    oi: int | None = None
    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()

    @property
    def best_bid(self) -> Decimal | None:
        return next((b.price for b in self.bids if b.price > 0 and b.quantity > 0), None)

    @property
    def best_ask(self) -> Decimal | None:
        return next((a.price for a in self.asks if a.price > 0 and a.quantity > 0), None)

    @property
    def mid(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None or a < b:
            return None
        return (a + b) / 2

    @property
    def spread(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else a - b


@dataclass(frozen=True)
class Tick:
    instrument_token: int
    last_price: float
    ts: datetime  # exchange timestamp
    volume_traded: int | None = None  # cumulative day volume, when the broker provides it
    oi: int | None = None


@dataclass(frozen=True)
class Bar:
    """OHLCV candle. ``ts`` is the candle OPEN time; a bar is only used once complete."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    oi: int | None = None


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str  # idempotency tag, alphanumeric, <= 20 chars (sent as Kite 'tag')
    exchange: str
    tradingsymbol: str
    instrument_token: int
    side: Side
    quantity: int
    order_type: str  # LIMIT | SL (stop-limit) | MARKET (exits only, with protection)
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    product: str = "MIS"  # intraday

    def __post_init__(self) -> None:
        if not self.client_order_id.isalnum() or len(self.client_order_id) > 20:
            raise ValueError("client_order_id must be alphanumeric and at most 20 chars")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type in ("LIMIT", "SL") and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("LIMIT/SL orders need a positive limit_price")
        if self.order_type == "SL" and self.trigger_price is None:
            raise ValueError("SL orders need a trigger_price")


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: str | None
    tradingsymbol: str
    side: Side
    quantity: int
    filled_quantity: int
    average_price: Decimal | None
    status: BrokerOrderStatus
    status_message: str | None = None
    updated_at: datetime | None = None

    @property
    def pending_quantity(self) -> int:
        return self.quantity - self.filled_quantity


@dataclass(frozen=True)
class BrokerTrade:
    broker_trade_id: str
    broker_order_id: str
    quantity: int
    price: Decimal
    ts: datetime


@dataclass(frozen=True)
class BrokerPosition:
    exchange: str
    tradingsymbol: str
    instrument_token: int
    quantity: int  # net; positive = long, negative = short (must never happen here)
    average_price: Decimal
    product: str


@dataclass(frozen=True)
class Funds:
    available_cash: Decimal
    used_margin: Decimal = Decimal(0)


@dataclass(frozen=True)
class Profile:
    user_id: str
    user_name: str
    broker: str


@dataclass
class OptionChainRow:
    instrument: Instrument
    quote: Quote | None = None
    extra: dict[str, float] = field(default_factory=dict)
