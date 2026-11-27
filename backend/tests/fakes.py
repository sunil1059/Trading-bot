"""Shared test fakes and builders. Shapes mirror the kiteconnect SDK's parsed responses."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from backend.core.clock import IST
from backend.models.domain import Bar, Instrument


class FakeKite:
    VARIETY_REGULAR = "regular"
    VALIDITY_DAY = "DAY"

    def __init__(self):
        self.placed: list[dict] = []
        self.raise_on: dict[str, Exception] = {}
        self.order_book: list[dict] = []
        self.quotes: dict = {}

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def profile(self):
        self._maybe_raise("profile")
        return {"user_id": "AB1234", "user_name": "Test"}

    def margins(self, segment):
        return {"net": 100000.5, "utilised": {"debits": 0}}

    def place_order(self, **kw):
        self._maybe_raise("place_order")
        self.placed.append(kw)
        return "2511270001"

    def orders(self):
        self._maybe_raise("orders")
        return self.order_book

    def quote(self, keys):
        return {k: self.quotes[k] for k in keys if k in self.quotes}

    def trades(self):
        return []

    def positions(self):
        return {"net": [], "day": []}

    def instruments(self, exchange):
        return []


def opt(token, strike, kind, expiry=date(2030, 1, 8), lot=65, name="NIFTY"):
    return Instrument(instrument_token=token, exchange="NFO", tradingsymbol=f"{name}{expiry:%y%m%d}{strike}{kind}",
                      name=name, instrument_type=kind, segment="NFO-OPT", lot_size=lot,
                      tick_size=Decimal("0.05"), expiry=expiry, strike=Decimal(strike))


def minute_bars(start: datetime, closes: list[float], volume: int = 1000, spread: float = 2.0) -> list[Bar]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Bar(ts=start + timedelta(minutes=i), open=o, high=max(o, c) + spread / 2,
                       low=min(o, c) - spread / 2, close=c, volume=volume))
        prev = c
    return out


def ist_dt(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


# --- scriptable broker for execution tests -----------------------------------------
from backend.brokers.base import BrokerBase  # noqa: E402
from backend.models.domain import (  # noqa: E402
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    Funds,
    Profile,
)


class ScriptedBroker(BrokerBase):
    """Orders follow a script of (status, filled) steps returned on successive status polls."""

    name = "scripted"

    def __init__(self, script=None, place_error=None, accept_despite_error=False):
        self.script = list(script or [(BrokerOrderStatus.FILLED, None)])
        self.place_error = place_error
        self.accept_despite_error = accept_despite_error
        self.orders: dict[str, BrokerOrder] = {}
        self.placed = []
        self.cancelled = []
        self.positions: list[BrokerPosition] = []
        self._n = 0

    def authenticate(self): ...
    def get_profile(self): return Profile("U", "u", self.name)
    def get_balance(self): return Funds(Decimal(100000))
    def get_instruments(self, exchange): return []
    def get_quote(self, instruments): return {}
    def get_historical_data(self, *a, **k): return []
    def get_trade_history(self): return []
    def get_positions(self): return self.positions
    def modify_order(self, *a, **k): ...

    def place_order(self, req):
        self._n += 1
        oid = f"B{self._n}"
        self.placed.append(req)
        self.orders[oid] = BrokerOrder(oid, req.client_order_id, req.tradingsymbol, req.side, req.quantity, 0,
                                       None, BrokerOrderStatus.OPEN)
        if self.place_error is not None:
            err, self.place_error = self.place_error, None
            if not self.accept_despite_error:
                del self.orders[oid]
            raise err
        return oid

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        o = self.orders[oid]
        if not o.status.is_terminal:
            o.status = BrokerOrderStatus.CANCELLED

    def get_order_book(self):
        return list(self.orders.values())

    def get_order_status(self, oid):
        o = self.orders[oid]
        if not o.status.is_terminal and self.script:
            status, filled = self.script.pop(0)
            o.filled_quantity = o.quantity if filled is None else filled
            if o.filled_quantity:
                o.average_price = Decimal("100")
            o.status = status
        return o


class FakeMarket(BrokerBase):
    """Deterministic market-data broker for engine tests. Option quotes are Black-Scholes
    prices around a controllable spot: test data only, never used outside tests."""

    name = "fakedata"

    def __init__(self, clock, bars, today, spot=24100.0, vol=0.13):
        from backend.models.domain import Instrument as I
        self.clock, self.bars, self.spot, self.vol = clock, bars, spot, vol
        self.expiry = today + timedelta(days=5)
        self.fut = I(900, "NFO", "NIFTYFUT", "NIFTY", "FUT", "NFO-FUT", 65, Decimal("0.1"), today + timedelta(days=25))
        self.index = I(256265, "NSE", "NIFTY 50", "", "EQ", "INDICES", 1, Decimal("0.05"))
        self.opts = [opt(1000 + i * 2 + j, 23500 + 50 * i, k, self.expiry)
                     for i in range(25) for j, k in enumerate(("CE", "PE"))]
        self.auth_fail = False
        self.spread = Decimal("0.4")

    def authenticate(self):
        from backend.brokers.base import AuthError
        if self.auth_fail:
            raise AuthError("expired")

    def get_profile(self): return Profile("U", "u", self.name)
    def get_balance(self): return Funds(Decimal(0))

    def get_instruments(self, exchange):
        return [self.fut, *self.opts] if exchange == "NFO" else [self.index]

    def get_historical_data(self, token, start, end, interval, oi=False):
        return [b for b in self.bars if start <= b.ts <= end]

    def option_price(self, inst):
        from backend.options.greeks import bs_price
        from backend.options.selection import years_to_expiry
        t = years_to_expiry(self.clock.now(), inst.expiry)
        return bs_price(OptType(inst.instrument_type), self.spot, float(inst.strike), t, 0.065, self.vol)

    def get_quote(self, instruments):
        from backend.models.domain import DepthLevel, Quote
        now = self.clock.now()
        out = {}
        for inst in instruments:
            if inst.is_option:
                mid = Decimal(str(round(self.option_price(inst), 1)))
                out[inst.instrument_token] = Quote(inst.instrument_token, mid, now, now, 900000, 300000,
                                                   (DepthLevel(mid - self.spread / 2, 1300),),
                                                   (DepthLevel(mid + self.spread / 2, 1300),))
            else:
                p = Decimal(str(self.spot))
                out[inst.instrument_token] = Quote(inst.instrument_token, p, now, now)
        return out

    def place_order(self, req): raise AssertionError("data broker must never receive orders")
    def modify_order(self, *a, **k): raise AssertionError
    def cancel_order(self, oid): raise AssertionError
    def get_order_book(self): return []
    def get_trade_history(self): return []
    def get_positions(self): return []


from backend.models.domain import OptType  # noqa: E402
