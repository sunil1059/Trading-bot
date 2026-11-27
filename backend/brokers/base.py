"""Unified broker interface. Strategy, risk and execution code depend only on this.

Implementations must:
- use only official, documented APIs;
- raise the typed errors below (never return fake data on failure);
- never retry an order placement themselves: the order manager owns idempotent retries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

from backend.models.domain import (
    Bar,
    BrokerOrder,
    BrokerPosition,
    BrokerTrade,
    Funds,
    Instrument,
    OptionChainRow,
    OrderRequest,
    Profile,
    Quote,
)


class BrokerError(Exception):
    """Base class. ``retryable`` tells callers whether a read call may be retried."""

    retryable = False


class AuthError(BrokerError):
    """Session missing, expired or revoked. Requires a fresh manual login."""


class RateLimitError(BrokerError):
    retryable = True


class NetworkError(BrokerError):
    """Timeout or connection failure. For order placement, outcome is UNKNOWN."""

    retryable = True


class OrderRejected(BrokerError):
    """Broker/exchange refused the order. Message is kept for the audit log."""


class InputError(BrokerError):
    pass


class DataError(BrokerError):
    """Broker returned data we cannot trust (missing fields, bad timestamps)."""


class BrokerBase(ABC):
    name: str = "base"

    # --- session ---------------------------------------------------------------
    @abstractmethod
    def authenticate(self) -> None:
        """Verify the current session is valid; raise AuthError otherwise."""

    @abstractmethod
    def get_profile(self) -> Profile: ...

    @abstractmethod
    def get_balance(self) -> Funds: ...

    def get_available_margin(self) -> Funds:
        return self.get_balance()

    # --- reference / market data -----------------------------------------------
    @abstractmethod
    def get_instruments(self, exchange: str) -> list[Instrument]: ...

    def get_expiries(self, exchange: str, underlying: str) -> list[date]:
        return sorted(
            {
                i.expiry
                for i in self.get_instruments(exchange)
                if i.name == underlying and i.is_option and i.expiry is not None
            }
        )

    @abstractmethod
    def get_quote(self, instruments: list[Instrument]) -> dict[int, Quote]: ...

    def get_option_chain(self, exchange: str, underlying: str, expiry: date) -> list[OptionChainRow]:
        contracts = [
            i
            for i in self.get_instruments(exchange)
            if i.name == underlying and i.is_option and i.expiry == expiry
        ]
        quotes = self.get_quote(contracts) if contracts else {}
        return [OptionChainRow(instrument=c, quote=quotes.get(c.instrument_token)) for c in contracts]

    @abstractmethod
    def get_historical_data(
        self, instrument_token: int, start: datetime, end: datetime, interval: str, oi: bool = False
    ) -> list[Bar]: ...

    # --- orders ------------------------------------------------------------------
    @abstractmethod
    def place_order(self, req: OrderRequest) -> str:
        """Submit an order and return the broker order id. Acceptance != execution."""

    @abstractmethod
    def modify_order(self, broker_order_id: str, *, limit_price=None, quantity=None, trigger_price=None) -> None: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    def get_order_book(self) -> list[BrokerOrder]: ...

    def get_order_status(self, broker_order_id: str) -> BrokerOrder:
        for o in self.get_order_book():
            if o.broker_order_id == broker_order_id:
                return o
        raise DataError(f"order {broker_order_id} not in order book")

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Idempotency lookup used after timeouts and on restart."""
        matches = [o for o in self.get_order_book() if o.client_order_id == client_order_id]
        if len(matches) > 1:
            raise DataError(f"multiple broker orders carry client id {client_order_id}")
        return matches[0] if matches else None

    @abstractmethod
    def get_trade_history(self) -> list[BrokerTrade]: ...

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...
