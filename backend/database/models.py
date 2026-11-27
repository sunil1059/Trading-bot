"""Database schema (SQLAlchemy 2). Works on SQLite (dev/tests) and PostgreSQL (Docker).

Safety invariants are also enforced at the database level where possible, e.g. an
ENTRY order must be a BUY, so a bug higher up cannot persist a short-opening order.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(14, 2)
Price = Numeric(12, 2)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, datetime: DateTime(timezone=True)}


# --- enums stored as strings -------------------------------------------------
class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class SignalDirection(StrEnum):
    BUY_CE = "BUY_CE"
    BUY_PE = "BUY_PE"
    NO_TRADE = "NO_TRADE"


class SignalStatus(StrEnum):
    GENERATED = "GENERATED"
    REJECTED = "REJECTED"
    ACCEPTED = "ACCEPTED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    STOP_BACKSTOP = "STOP_BACKSTOP"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"  # persisted before the API call (idempotency)
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # API timed out; must be reconciled before any retry


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


def _enum_check(col: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{e.value}'" for e in enum)
    return f"{col} IN ({values})"


# --- tables ------------------------------------------------------------------
class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("instrument_token", "interval", "ts", name="uq_candle"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    instrument_token: Mapped[int] = mapped_column(BigInteger, index=True)
    interval: Mapped[str] = mapped_column(String(16))
    ts: Mapped[datetime] = mapped_column(comment="Candle OPEN time, IST-aware")
    open: Mapped[Decimal] = mapped_column(Price)
    high: Mapped[Decimal] = mapped_column(Price)
    low: Mapped[Decimal] = mapped_column(Price)
    close: Mapped[Decimal] = mapped_column(Price)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    oi: Mapped[int | None] = mapped_column(BigInteger)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint(_enum_check("direction", SignalDirection), name="ck_signal_direction"),
        CheckConstraint(_enum_check("status", SignalStatus), name="ck_signal_status"),
        CheckConstraint("score >= 0 AND score <= 100", name="ck_signal_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    mode: Mapped[str] = mapped_column(String(16))
    underlying: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    score: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text, comment="Why rejected / accepted")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)

    orders: Mapped[list[Order]] = relationship(back_populates="signal")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint(_enum_check("option_type", OptionType), name="ck_position_option_type"),
        CheckConstraint(_enum_check("status", PositionStatus), name="ck_position_status"),
        CheckConstraint("quantity >= 0", name="ck_position_qty_nonneg"),  # long-only
        CheckConstraint("lot_size > 0", name="ck_position_lot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    exchange: Mapped[str] = mapped_column(String(8))
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    instrument_token: Mapped[int] = mapped_column(BigInteger)
    underlying: Mapped[str] = mapped_column(String(32))
    option_type: Mapped[str] = mapped_column(String(2))
    strike: Mapped[Decimal] = mapped_column(Price)
    expiry: Mapped[date] = mapped_column(Date)
    lot_size: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer, comment="Verified with broker; never negative")
    avg_entry_price: Mapped[Decimal] = mapped_column(Price)
    stop_loss: Mapped[Decimal | None] = mapped_column(Price)
    target: Mapped[Decimal | None] = mapped_column(Price)
    status: Mapped[str] = mapped_column(String(16), index=True)
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]

    orders: Mapped[list[Order]] = relationship(back_populates="position")
    trade: Mapped[Trade | None] = relationship(back_populates="position")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_enum_check("side", OrderSide), name="ck_order_side"),
        CheckConstraint(_enum_check("purpose", OrderPurpose), name="ck_order_purpose"),
        CheckConstraint(_enum_check("status", OrderStatus), name="ck_order_status"),
        # Long-only invariant: opening orders are always BUY; closing orders are always SELL.
        CheckConstraint("(purpose = 'ENTRY' AND side = 'BUY') OR (purpose != 'ENTRY' AND side = 'SELL')",
                        name="ck_order_long_only"),
        CheckConstraint("quantity > 0", name="ck_order_qty_pos"),
        CheckConstraint("filled_quantity >= 0 AND filled_quantity <= quantity", name="ck_order_filled"),
        Index("ix_orders_status_mode", "status", "mode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(32), unique=True,
                                                 comment="Our idempotency tag, sent to broker as order tag")
    broker_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    mode: Mapped[str] = mapped_column(String(16))
    exchange: Mapped[str] = mapped_column(String(8))
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    instrument_token: Mapped[int] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(4))
    purpose: Mapped[str] = mapped_column(String(16))
    order_type: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal | None] = mapped_column(Price)
    trigger_price: Mapped[Decimal | None] = mapped_column(Price)
    status: Mapped[str] = mapped_column(String(20))
    status_message: Mapped[str | None] = mapped_column(Text)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_price: Mapped[Decimal | None] = mapped_column(Price)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"))
    signal: Mapped[Signal | None] = relationship(back_populates="orders")
    position: Mapped[Position | None] = relationship(back_populates="orders")
    executions: Mapped[list[Execution]] = relationship(back_populates="order")


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_exec_qty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    broker_trade_id: Mapped[str] = mapped_column(String(64), unique=True)
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Price)
    ts: Mapped[datetime]

    order: Mapped[Order] = relationship(back_populates="executions")


class Trade(Base):
    """A completed round trip: one position from entry to final exit."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), unique=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    entry_time: Mapped[datetime]
    exit_time: Mapped[datetime]
    quantity: Mapped[int] = mapped_column(Integer)
    avg_entry_price: Mapped[Decimal] = mapped_column(Price)
    avg_exit_price: Mapped[Decimal] = mapped_column(Price)
    gross_pnl: Mapped[Decimal] = mapped_column(Money)
    charges: Mapped[Decimal] = mapped_column(Money)
    net_pnl: Mapped[Decimal] = mapped_column(Money)
    exit_reason: Mapped[str] = mapped_column(String(32))

    position: Mapped[Position] = relationship(back_populates="trade")


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    mode: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(48), comment="e.g. DAILY_LOSS_LIMIT, STALE_DATA, EMERGENCY_STOP")
    severity: Mapped[str] = mapped_column(String(8))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class BrokerEvent(Base):
    __tablename__ = "broker_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    broker: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32), comment="e.g. API_ERROR, RATE_LIMIT, WS_DISCONNECT")
    endpoint: Mapped[str | None] = mapped_column(String(128))
    status_code: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, comment="Redacted before storage")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(String(64), comment="'system' or the dashboard user")
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SettingsSnapshot(Base):
    """Risk config in force, recorded at every start and every change."""

    __tablename__ = "settings_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime]
    mode: Mapped[str] = mapped_column(String(16))
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    risk_config: Mapped[dict[str, Any]]


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    strategy: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    is_out_of_sample: Mapped[bool]
    params: Mapped[dict[str, Any]]
    metrics: Mapped[dict[str, Any]]
