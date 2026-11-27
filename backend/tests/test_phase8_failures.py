"""Phase 8: failure scenarios end to end (paper broker over the fake market)."""

from datetime import timedelta
from decimal import Decimal

from backend.brokers.base import AuthError, NetworkError, OrderRejected
from backend.core.config import TradingMode
from backend.database.models import Order, Position, RiskEvent, Signal, Trade
from backend.models.domain import BrokerPosition
from backend.tests.test_phase6_engine import _rows, world  # noqa: F401  (pytest fixture)


def _enter(world):  # noqa: F811
    eng, paper = world["build"]()
    eng.start()
    world["tick"]()
    eng.step()
    assert len(eng.open) == 1
    return eng, paper


def _later(world, eng, seconds=5):  # noqa: F811
    world["clock"].advance(timedelta(seconds=seconds))
    world["tick"]()
    eng.step()


def test_quote_api_down_with_open_position_does_not_crash_and_exits_when_back(world, monkeypatch):  # noqa: F811
    eng, paper = _enter(world)
    real = world["market"].get_quote

    def down(_):
        raise NetworkError("connection reset")

    monkeypatch.setattr(world["market"], "get_quote", down)
    for _ in range(3):
        _later(world, eng)  # must not raise
    assert paper.get_positions()[0].quantity > 0  # paper never fills without fresh quotes
    assert any(e.kind == "EXIT_INCOMPLETE" for e in _rows(world["sessions"], RiskEvent))
    monkeypatch.setattr(world["market"], "get_quote", real)
    world["market"].spot -= 200  # meanwhile the market fell through the stop
    _later(world, eng)
    assert paper.get_positions()[0].quantity == 0
    assert len(_rows(world["sessions"], Trade)) == 1


def test_token_expiry_mid_session_blocks_new_entries(world, monkeypatch):  # noqa: F811
    eng, paper = world["build"]()
    eng.start()

    def expired(_):
        raise AuthError("TokenException: session expired")

    monkeypatch.setattr(world["market"], "get_quote", expired)
    eng.auth_ok = False  # what the runtime loop sets on AuthError
    world["tick"]()
    eng.step()
    assert not _rows(world["sessions"], Order)
    sigs = _rows(world["sessions"], Signal)
    assert all(s.status != "ACCEPTED" for s in sigs)


def test_order_rejected_by_broker_leaves_no_position(world, monkeypatch):  # noqa: F811
    eng, paper = world["build"]()
    eng.start()

    def reject(req):
        raise OrderRejected("RMS: margin exceeds")

    monkeypatch.setattr(paper, "place_order", reject)
    world["tick"]()
    eng.step()
    assert not eng.open and not _rows(world["sessions"], Position)
    orders = _rows(world["sessions"], Order)
    assert len(orders) == 1 and orders[0].status == "REJECTED"
    _later(world, eng, 60)  # no automatic retry storm: at most one attempt per signal bar
    assert len(_rows(world["sessions"], Order)) == 1


def test_unknown_position_at_broker_triggers_emergency_lock(world, monkeypatch):  # noqa: F811
    eng, paper = world["build"]()
    eng.start()
    stranger = BrokerPosition("NFO", "NIFTYXYZCE", 1, 65, Decimal(100), "MIS")
    monkeypatch.setattr(paper, "get_positions", lambda: [stranger])
    eng.reconcile(force=True)
    assert eng.risk.state.emergency_lock
    world["tick"]()
    eng.step()
    assert not _rows(world["sessions"], Order)


def test_short_position_at_broker_is_critical(world, monkeypatch):  # noqa: F811
    eng, paper = _enter(world)
    sym = next(iter(eng.open.values())).inst.tradingsymbol
    monkeypatch.setattr(paper, "get_positions", lambda: [BrokerPosition("NFO", sym, 1, -65, Decimal(100), "MIS")])
    eng.reconcile(force=True)
    assert eng.risk.state.emergency_lock
    assert any(e.kind == "RECONCILE_CRITICAL" for e in _rows(world["sessions"], RiskEvent))


def test_position_closed_outside_bot_is_adopted(world, monkeypatch):  # noqa: F811
    eng, paper = _enter(world)
    monkeypatch.setattr(paper, "get_positions", lambda: [])
    eng.reconcile(force=True)
    assert not eng.open
    assert not eng.risk.state.emergency_lock


def test_restart_after_crash_never_duplicates_entry(world):  # noqa: F811
    eng, paper = _enter(world)
    for _ in range(3):  # crash and restart three times within the same signal bar
        eng2, _ = world["build"]()
        eng2.broker = eng2.om.broker = paper
        eng2.start()
        world["tick"]()
        eng2.step()
    entries = [o for o in _rows(world["sessions"], Order) if o.purpose == "ENTRY"]
    assert len(entries) == 1


def test_emergency_lock_survives_restart(world):  # noqa: F811
    eng, paper = _enter(world)
    eng.emergency_stop("tester")
    eng2, _ = world["build"]()
    eng2.broker = eng2.om.broker = paper
    eng2.start()
    assert eng2.risk.state.emergency_lock


def test_signal_only_never_touches_order_api(world, monkeypatch):  # noqa: F811
    eng, paper = world["build"](TradingMode.SIGNAL_ONLY)

    def boom(*a, **k):
        raise AssertionError("order API used in SIGNAL_ONLY")

    monkeypatch.setattr(paper, "place_order", boom)
    eng.start()
    world["tick"]()
    eng.step()
    assert _rows(world["sessions"], Signal)


def test_position_closed_outside_bot_books_conservative_loss(world, monkeypatch):  # noqa: F811
    eng, paper = _enter(world)
    t = next(iter(eng.open.values()))
    stop, entry, qty = t.plan.stop, t.plan.entry_price, t.plan.quantity
    monkeypatch.setattr(paper, "get_positions", lambda: [])
    eng.reconcile(force=True)
    tr = _rows(world["sessions"], Trade)[0]
    assert tr.avg_exit_price == stop and tr.gross_pnl == (stop - entry) * qty
    assert eng.risk.state.realised_pnl_today == tr.net_pnl < 0
    assert any(e.kind == "EXIT_PRICE_UNKNOWN" for e in _rows(world["sessions"], RiskEvent))
