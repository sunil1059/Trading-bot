"""Zerodha Kite Connect adapter, built on the official ``kiteconnect`` SDK.

Facts this relies on (Kite Connect docs / Zerodha support, checked 2026-11-27):
- Access tokens last for the trading day and expire before the next open; login is a
  manual browser flow every day (no refresh-token automation).
- Order placement is limited to 10 orders/second per client account and must come from
  a registered static IP. We self-limit far below that.
- Market orders require market protection; protection 0 is rejected. We only use MARKET
  for emergency exits, with automatic protection (-1).
- No sandbox exists: this adapter is only exercised against fakes in tests.

Field names follow the SDK's parsed responses. Kite returns naive IST datetimes; we
attach Asia/Kolkata explicitly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from backend.brokers.base import (
    AuthError,
    BrokerBase,
    BrokerError,
    DataError,
    InputError,
    NetworkError,
    OrderRejected,
    RateLimitError,
)
from backend.brokers.rate_limit import RateLimiter, retry_read
from backend.core.clock import IST
from backend.logging.setup import register_secret
from backend.models.domain import (
    Bar,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerTrade,
    DepthLevel,
    Funds,
    Instrument,
    OrderRequest,
    Profile,
    Quote,
    Side,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

# Kite status strings -> our status. Anything not listed is treated as still OPEN.
_TERMINAL = {"COMPLETE": BrokerOrderStatus.FILLED, "CANCELLED": BrokerOrderStatus.CANCELLED,
             "REJECTED": BrokerOrderStatus.REJECTED}
_INTERVALS = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute", "30m": "30minute",
              "60m": "60minute", "1d": "day"}


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


def _ist(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if not isinstance(v, datetime):
        raise DataError(f"unexpected timestamp {v!r}")
    return v.replace(tzinfo=IST) if v.tzinfo is None else v.astimezone(IST)


class ZerodhaBroker(BrokerBase):
    name = "zerodha"

    def __init__(self, kite: Any, *, order_limiter: RateLimiter | None = None,
                 quote_limiter: RateLimiter | None = None, hist_limiter: RateLimiter | None = None) -> None:
        """``kite`` is a ``kiteconnect.KiteConnect`` with the access token already set."""
        self._kite = kite
        # Conservative self-limits, well under documented broker limits.
        self._order_limiter = order_limiter or RateLimiter(5, 1.0)
        self._quote_limiter = quote_limiter or RateLimiter(1, 1.0)
        self._hist_limiter = hist_limiter or RateLimiter(2, 1.0)
        self._instrument_cache: dict[str, list[Instrument]] = {}

    @classmethod
    def from_credentials(cls, api_key: str, access_token: str) -> ZerodhaBroker:
        from kiteconnect import KiteConnect

        register_secret(access_token)
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        return cls(kite)

    # --- error mapping ---------------------------------------------------------------
    def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        from kiteconnect import exceptions as kex
        from requests import exceptions as rex

        try:
            return fn(*args, **kwargs)
        except kex.TokenException as e:
            raise AuthError(str(e)) from e
        except kex.PermissionException as e:
            raise AuthError(f"permission denied: {e}") from e
        except kex.OrderException as e:
            raise OrderRejected(str(e)) from e
        except kex.InputException as e:
            raise InputError(str(e)) from e
        except kex.DataException as e:
            raise DataError(str(e)) from e
        except kex.NetworkException as e:
            if getattr(e, "code", None) == 429:
                raise RateLimitError(str(e)) from e
            raise NetworkError(str(e)) from e
        except kex.KiteException as e:
            if getattr(e, "code", None) == 429:
                raise RateLimitError(str(e)) from e
            raise BrokerError(str(e)) from e
        except (rex.Timeout, rex.ConnectionError) as e:
            raise NetworkError(str(e)) from e

    def _read(self, limiter: RateLimiter, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        def once() -> T:
            limiter.acquire()
            return self._call(fn, *args, **kwargs)

        return retry_read(once)

    # --- session ---------------------------------------------------------------------
    def authenticate(self) -> None:
        self.get_profile()

    def get_profile(self) -> Profile:
        p = self._read(self._quote_limiter, self._kite.profile)
        return Profile(user_id=p["user_id"], user_name=p.get("user_name", ""), broker=self.name)

    def get_balance(self) -> Funds:
        m = self._read(self._quote_limiter, self._kite.margins, "equity")
        try:
            return Funds(available_cash=_d(m["net"]), used_margin=_d(m["utilised"]["debits"]))
        except KeyError as e:
            raise DataError(f"margins response missing {e}") from e

    # --- reference data --------------------------------------------------------------
    def get_instruments(self, exchange: str) -> list[Instrument]:
        if exchange not in self._instrument_cache:
            rows = self._read(self._hist_limiter, self._kite.instruments, exchange)
            self._instrument_cache[exchange] = [self._to_instrument(r) for r in rows]
        return self._instrument_cache[exchange]

    def refresh_instruments(self) -> None:
        """Call once per trading day before the open: lot sizes and expiries change."""
        self._instrument_cache.clear()

    @staticmethod
    def _to_instrument(r: dict[str, Any]) -> Instrument:
        expiry = r.get("expiry") or None
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        strike = r.get("strike")
        return Instrument(
            instrument_token=int(r["instrument_token"]),
            exchange=r["exchange"],
            tradingsymbol=r["tradingsymbol"],
            name=r.get("name") or "",
            instrument_type=r["instrument_type"],
            segment=r.get("segment", ""),
            lot_size=int(r["lot_size"]),
            tick_size=_d(r["tick_size"]),
            expiry=expiry if isinstance(expiry, date) else None,
            strike=_d(strike) if strike not in (None, "", 0, 0.0) else None,
        )

    # --- market data -----------------------------------------------------------------
    def get_quote(self, instruments: list[Instrument]) -> dict[int, Quote]:
        out: dict[int, Quote] = {}
        keys = [f"{i.exchange}:{i.tradingsymbol}" for i in instruments]
        for start in range(0, len(keys), 250):  # keep requests modest
            raw = self._read(self._quote_limiter, self._kite.quote, keys[start:start + 250])
            for q in raw.values():
                quote = self._to_quote(q)
                out[quote.instrument_token] = quote
        return out

    @staticmethod
    def _to_quote(q: dict[str, Any]) -> Quote:
        ts = _ist(q.get("timestamp"))
        if ts is None:
            raise DataError("quote without exchange timestamp")
        depth = q.get("depth") or {}

        def levels(side: str) -> tuple[DepthLevel, ...]:
            return tuple(
                DepthLevel(price=_d(lv["price"]), quantity=int(lv["quantity"]), orders=int(lv.get("orders", 0)))
                for lv in depth.get(side, [])
            )

        return Quote(
            instrument_token=int(q["instrument_token"]),
            last_price=_d(q["last_price"]),
            ts=ts,
            last_trade_time=_ist(q.get("last_trade_time")),
            volume=int(q.get("volume") or 0),
            oi=int(q["oi"]) if q.get("oi") is not None else None,
            bids=levels("buy"),
            asks=levels("sell"),
        )

    def get_historical_data(self, instrument_token: int, start: datetime, end: datetime, interval: str,
                            oi: bool = False) -> list[Bar]:
        if interval not in _INTERVALS:
            raise InputError(f"unsupported interval {interval}")
        rows = self._read(self._hist_limiter, self._kite.historical_data, instrument_token,
                          start.astimezone(IST).replace(tzinfo=None), end.astimezone(IST).replace(tzinfo=None),
                          _INTERVALS[interval], False, oi)
        bars = []
        for r in rows:
            ts = _ist(r["date"])
            assert ts is not None
            bars.append(Bar(ts=ts, open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                            close=float(r["close"]), volume=int(r.get("volume") or 0),
                            oi=int(r["oi"]) if oi and r.get("oi") is not None else None))
        return bars

    # --- orders ----------------------------------------------------------------------
    def place_order(self, req: OrderRequest) -> str:
        if req.order_type not in ("LIMIT", "SL", "MARKET"):
            raise InputError(f"order type {req.order_type} not allowed")
        if req.order_type == "MARKET" and req.side is Side.BUY:
            raise InputError("MARKET orders are never used for entries")
        kwargs: dict[str, Any] = dict(
            variety=self._kite.VARIETY_REGULAR,
            exchange=req.exchange,
            tradingsymbol=req.tradingsymbol,
            transaction_type=req.side.value,
            quantity=req.quantity,
            product=req.product,
            order_type=req.order_type,
            validity=self._kite.VALIDITY_DAY,
            tag=req.client_order_id,
        )
        if req.limit_price is not None:
            kwargs["price"] = float(req.limit_price)
        if req.trigger_price is not None:
            kwargs["trigger_price"] = float(req.trigger_price)
        if req.order_type == "MARKET":
            kwargs["market_protection"] = -1  # automatic protection; 0 is rejected by Kite
        self._order_limiter.acquire()
        # Deliberately NOT retried: a timeout leaves the outcome unknown. The order manager
        # reconciles by client_order_id (tag) before deciding anything.
        return str(self._call(self._kite.place_order, **kwargs))

    def modify_order(self, broker_order_id: str, *, limit_price=None, quantity=None, trigger_price=None) -> None:
        self._order_limiter.acquire()
        self._call(self._kite.modify_order, variety=self._kite.VARIETY_REGULAR, order_id=broker_order_id,
                   price=float(limit_price) if limit_price is not None else None, quantity=quantity,
                   trigger_price=float(trigger_price) if trigger_price is not None else None)

    def cancel_order(self, broker_order_id: str) -> None:
        self._order_limiter.acquire()
        self._call(self._kite.cancel_order, variety=self._kite.VARIETY_REGULAR, order_id=broker_order_id)

    def get_order_book(self) -> list[BrokerOrder]:
        return [self._to_order(o) for o in self._read(self._quote_limiter, self._kite.orders)]

    @staticmethod
    def _to_order(o: dict[str, Any]) -> BrokerOrder:
        filled = int(o.get("filled_quantity") or 0)
        raw = str(o.get("status") or "").upper()
        status = _TERMINAL.get(raw)
        if status is None:
            status = BrokerOrderStatus.PARTIALLY_FILLED if filled > 0 else BrokerOrderStatus.OPEN
        avg = o.get("average_price")
        return BrokerOrder(
            broker_order_id=str(o["order_id"]),
            client_order_id=o.get("tag"),
            tradingsymbol=o["tradingsymbol"],
            side=Side(o["transaction_type"]),
            quantity=int(o["quantity"]),
            filled_quantity=filled,
            average_price=_d(avg) if avg else None,
            status=status,
            status_message=o.get("status_message"),
            updated_at=_ist(o.get("exchange_update_timestamp") or o.get("order_timestamp")),
        )

    def get_trade_history(self) -> list[BrokerTrade]:
        out = []
        for t in self._read(self._quote_limiter, self._kite.trades):
            ts = _ist(t.get("fill_timestamp") or t.get("exchange_timestamp"))
            if ts is None:
                raise DataError("trade without timestamp")
            out.append(BrokerTrade(broker_trade_id=str(t["trade_id"]), broker_order_id=str(t["order_id"]),
                                   quantity=int(t["quantity"]), price=_d(t["average_price"]), ts=ts))
        return out

    def get_positions(self) -> list[BrokerPosition]:
        raw = self._read(self._quote_limiter, self._kite.positions)
        return [
            BrokerPosition(exchange=p["exchange"], tradingsymbol=p["tradingsymbol"],
                           instrument_token=int(p["instrument_token"]), quantity=int(p["quantity"]),
                           average_price=_d(p["average_price"]), product=p["product"])
            for p in raw.get("net", [])
        ]


def login_url(api_key: str) -> str:
    from kiteconnect import KiteConnect

    return KiteConnect(api_key=api_key).login_url()


def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> str:
    """Complete the daily manual login. Returns the access token (never log it)."""
    from kiteconnect import KiteConnect
    from kiteconnect import exceptions as kex

    kite = KiteConnect(api_key=api_key)
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except kex.KiteException as e:
        raise AuthError(f"login failed: {e}") from e
    token = data["access_token"]
    register_secret(token)
    return token
