from datetime import date, timedelta
from decimal import Decimal

import pytest
from kiteconnect import exceptions as kex

from backend.brokers.base import AuthError, DataError, InputError, NetworkError, OrderRejected, RateLimitError
from backend.brokers.rate_limit import RateLimiter, retry_read
from backend.brokers.zerodha import ZerodhaBroker
from backend.core.clock import FixedClock
from backend.market_data.candles import TickAggregator, bucket_start, resample
from backend.market_data.health import DataHealth
from backend.market_data.instruments import InstrumentError, OptionUniverse
from backend.models.domain import BrokerOrderStatus, OptType, OrderRequest, Side, Tick
from backend.tests.fakes import FakeKite, ist_dt, minute_bars, opt

NOLIMIT = dict(order_limiter=RateLimiter(1000), quote_limiter=RateLimiter(1000), hist_limiter=RateLimiter(1000))


def _req(**kw):
    base = dict(client_order_id="E1A2B3", exchange="NFO", tradingsymbol="NIFTYX", instrument_token=1,
                side=Side.BUY, quantity=65, order_type="LIMIT", limit_price=Decimal("101.5"))
    base.update(kw)
    return OrderRequest(**base)


# --- Zerodha adapter ---------------------------------------------------------------
def test_place_order_sends_tag_and_limit():
    k = FakeKite()
    b = ZerodhaBroker(k, **NOLIMIT)
    assert b.place_order(_req()) == "2511270001"
    sent = k.placed[0]
    assert sent["tag"] == "E1A2B3" and sent["order_type"] == "LIMIT" and sent["price"] == 101.5
    assert sent["transaction_type"] == "BUY" and "market_protection" not in sent


def test_market_entry_refused_and_market_exit_gets_protection():
    k = FakeKite()
    b = ZerodhaBroker(k, **NOLIMIT)
    with pytest.raises(InputError):
        b.place_order(_req(order_type="MARKET", limit_price=None))
    b.place_order(_req(side=Side.SELL, order_type="MARKET", limit_price=None))
    assert k.placed[-1]["market_protection"] == -1


def test_place_order_timeout_is_not_retried():
    k = FakeKite()
    k.raise_on["place_order"] = kex.NetworkException("timeout")
    b = ZerodhaBroker(k, **NOLIMIT)
    with pytest.raises(NetworkError):
        b.place_order(_req())
    assert k.placed == []


@pytest.mark.parametrize("exc,expected", [
    (kex.TokenException("expired"), AuthError),
    (kex.OrderException("rejected: margin"), OrderRejected),
    (kex.NetworkException("too many", code=429), RateLimitError),
])
def test_error_mapping(exc, expected):
    k = FakeKite()
    k.raise_on["place_order"] = exc
    with pytest.raises(expected):
        ZerodhaBroker(k, **NOLIMIT).place_order(_req())


def test_order_status_mapping_and_partial():
    k = FakeKite()
    k.order_book = [
        dict(order_id="1", tag="A1", tradingsymbol="X", transaction_type="BUY", quantity=130, filled_quantity=65,
             average_price=100.0, status="OPEN", status_message=None, order_timestamp=None),
        dict(order_id="2", tag="A2", tradingsymbol="X", transaction_type="SELL", quantity=65, filled_quantity=65,
             average_price=110.0, status="COMPLETE", status_message=None, order_timestamp=None),
        dict(order_id="3", tag=None, tradingsymbol="X", transaction_type="BUY", quantity=65, filled_quantity=0,
             average_price=0, status="REJECTED", status_message="Insufficient funds", order_timestamp=None),
    ]
    book = {o.broker_order_id: o for o in ZerodhaBroker(k, **NOLIMIT).get_order_book()}
    assert book["1"].status is BrokerOrderStatus.PARTIALLY_FILLED and book["1"].pending_quantity == 65
    assert book["2"].status is BrokerOrderStatus.FILLED
    assert book["3"].status is BrokerOrderStatus.REJECTED and book["3"].average_price is None


def test_find_by_client_id_detects_duplicates():
    k = FakeKite()
    row = dict(order_id="1", tag="DUP", tradingsymbol="X", transaction_type="BUY", quantity=65, filled_quantity=0,
               average_price=0, status="OPEN", order_timestamp=None)
    k.order_book = [row, {**row, "order_id": "2"}]
    with pytest.raises(DataError):
        ZerodhaBroker(k, **NOLIMIT).find_order_by_client_id("DUP")


def test_quote_parsing_requires_timestamp():
    k = FakeKite()
    k.quotes["NFO:X"] = {"instrument_token": 5, "last_price": 100, "timestamp": ist_dt(2030, 1, 1, 10, 0),
                         "depth": {"buy": [{"price": 99.9, "quantity": 650, "orders": 3}],
                                   "sell": [{"price": 100.1, "quantity": 650, "orders": 2}]}}
    inst = opt(5, 24000, "CE")
    inst = inst.__class__(**{**inst.__dict__, "tradingsymbol": "X"})
    q = ZerodhaBroker(k, **NOLIMIT).get_quote([inst])[5]
    assert q.best_bid == Decimal("99.9") and q.best_ask == Decimal("100.1") and q.mid == Decimal("100.0")
    k.quotes["NFO:X"]["timestamp"] = None
    with pytest.raises(DataError):
        ZerodhaBroker(k, **NOLIMIT).get_quote([inst])


def test_retry_read_only_retries_retryable():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise NetworkError("x")
        return "ok"

    assert retry_read(flaky, sleep=lambda s: None) == "ok"
    with pytest.raises(AuthError):
        retry_read(lambda: (_ for _ in ()).throw(AuthError("no")), sleep=lambda s: None)


def test_rate_limiter_blocks_excess():
    t = [0.0]
    slept = []
    lim = RateLimiter(2, 1.0, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    for _ in range(3):
        lim.acquire()
    assert slept and t[0] >= 1.0


# --- instruments -----------------------------------------------------------------
def test_option_universe():
    ex = date(2030, 1, 8)
    insts = [opt(i, 24000 + 50 * i, k) for i in range(5) for k in ("CE", "PE")]
    u = OptionUniverse.build(insts, "NIFTY")
    assert u.strike_step(ex) == Decimal(50)
    assert u.lot_size(ex) == 65
    assert u.atm_strike(ex, 24074) == Decimal(24050)
    assert u.contract(ex, Decimal(24100), OptType.PE).instrument_type == "PE"
    with pytest.raises(InstrumentError):
        OptionUniverse.build(insts, "BANKNIFTY")


# --- candles ---------------------------------------------------------------------
def test_bucket_alignment():
    assert bucket_start(ist_dt(2030, 1, 1, 9, 19, 59), 5) == ist_dt(2030, 1, 1, 9, 15)
    assert bucket_start(ist_dt(2030, 1, 1, 9, 30), 15) == ist_dt(2030, 1, 1, 9, 30)


def test_tick_aggregator_emits_only_completed_bars():
    agg = TickAggregator(1)
    t0 = ist_dt(2030, 1, 1, 9, 15, 1)
    assert agg.on_tick(Tick(1, 100.0, t0, volume_traded=1000)) == []
    assert agg.on_tick(Tick(1, 102.0, t0 + timedelta(seconds=30), volume_traded=1500)) == []
    assert agg.on_tick(Tick(1, 99.0, t0 + timedelta(seconds=20))) == []  # out of order
    assert agg.dropped == 1
    bars = agg.on_tick(Tick(1, 101.0, t0 + timedelta(seconds=61), volume_traded=1700))
    assert len(bars) == 1
    b = bars[0]
    assert (b.open, b.high, b.low, b.close, b.volume) == (100.0, 102.0, 100.0, 102.0, 500)
    assert agg.close_until(ist_dt(2030, 1, 1, 9, 16, 30)) == []
    assert len(agg.close_until(ist_dt(2030, 1, 1, 9, 17))) == 1


def test_resample_drops_incomplete_last_bucket():
    bars = minute_bars(ist_dt(2030, 1, 1, 9, 15), [100 + i for i in range(13)])
    five = resample(bars, 5)
    assert [b.ts.minute for b in five] == [15, 20]  # 09:25 bucket has only 3 of 5 minutes
    assert five[0].close == 104 and five[0].volume == 5000
    assert len(resample(bars, 5, now=ist_dt(2030, 1, 1, 9, 30))) == 3


# --- data health -----------------------------------------------------------------
def test_data_health():
    clock = FixedClock(ist_dt(2030, 1, 1, 10, 0))
    h = DataHealth(clock=clock)
    assert h.problems([1])  # disconnected + no ticks
    h.on_connect()
    assert h.record(1, ist_dt(2030, 1, 1, 9, 59, 58), 100.0)
    assert not h.record(1, ist_dt(2030, 1, 1, 9, 59, 58), 100.0)  # duplicate
    assert not h.record(1, ist_dt(2030, 1, 1, 9, 59, 50), 99.0)  # out of order
    assert not h.record(1, ist_dt(2030, 1, 1, 10, 1, 0), 99.0)  # from the future
    assert h.problems([1]) == []
    clock.advance(timedelta(seconds=10))
    assert h.problems([1]) == ["stale or missing ticks for 1"]
