from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from backend.brokers.base import NetworkError, OrderRejected
from backend.core.clock import IST, FixedClock
from backend.core.config import RiskConfig
from backend.database.models import Base, Order
from backend.database.session import make_engine, make_session_factory
from backend.execution.order_manager import ExecutionError, OrderManager
from backend.models.domain import BrokerOrderStatus, BrokerPosition, OptType
from backend.positions.exits import MarketView, TradePlan, evaluate
from backend.positions.reconcile import reconcile
from backend.risk.charges import ChargeRates, leg_charges, round_trip_charges
from backend.risk.engine import EntryContext, RiskDecision, RiskEngine, RiskState
from backend.strategies.config import ExitParams
from backend.tests.fakes import ScriptedBroker, opt

NOW = datetime(2030, 1, 3, 10, 30, tzinfo=IST)
RATES = ChargeRates.from_yaml()


# --- charges: matches the hand calculation in the Phase 1 doc -------------------------
def test_round_trip_charges_one_lot_at_150():
    total = round_trip_charges(RATES, Decimal(150), Decimal(150), 65)
    # brokerage 40 + STT 14.63 + txn 6.93 + SEBI 0.02 + stamp 0.29 + GST ~8.45 (+ IPFT ~0)
    assert Decimal("70.0") <= total <= Decimal("70.7")
    assert leg_charges(RATES, Decimal(150), 65, True) < leg_charges(RATES, Decimal(150), 65, False)


# --- risk engine -----------------------------------------------------------------
def _engine():
    return RiskEngine(RiskConfig.from_yaml(), RATES, RiskState(day=NOW.date(), week="2030-W01"))


def _ctx(**kw):
    base = dict(now=NOW, direction=OptType.CE, score=80, entry_price=Decimal(100), stop_price=Decimal(75),
                exit_slippage=Decimal("0.5"), lot_size=65, available_funds=Decimal(100000), open_positions=0,
                pending_entries=0, has_position_in_contract=False, data_problems=(), is_trading_day=True,
                is_expiry_day=False)
    base.update(kw)
    return EntryContext(**base)


def test_approves_one_lot_within_limits():
    d = _engine().check_entry(_ctx())
    assert d.approved and d.lots == 1 and d.quantity == 65
    assert d.max_loss <= Decimal(2500)


def test_rejects_when_one_lot_risk_too_big():
    d = _engine().check_entry(_ctx(entry_price=Decimal(200), stop_price=Decimal(150)))
    assert not d.approved and "exceeds limits" in d.reasons[0]


@pytest.mark.parametrize("kw,msg", [
    (dict(score=70), "score"),
    (dict(now=NOW.replace(hour=9, minute=25)), "entry window"),
    (dict(now=NOW.replace(hour=14, minute=40)), "entry window"),
    (dict(open_positions=1), "max open positions"),
    (dict(data_problems=("stale",)), "data"),
    (dict(is_expiry_day=True), "expiry day"),
    (dict(has_position_in_contract=True), "duplicate"),
    (dict(unreconciled_orders=1), "unreconciled"),
    (dict(stop_price=Decimal(100)), "invalid stop"),
    (dict(is_trading_day=False), "not a trading day"),
])
def test_rejections(kw, msg):
    d = _engine().check_entry(_ctx(**kw))
    assert not d.approved and any(msg in r for r in d.reasons), d.reasons


def test_consecutive_losses_pause_then_stop_and_cooldown():
    e = _engine()
    assert e.on_trade_closed(Decimal(-500), OptType.CE, "stop_loss", NOW) == []
    assert "PAUSED" in e.on_trade_closed(Decimal(-500), OptType.CE, "stop_loss", NOW)
    assert not e.check_entry(_ctx(now=NOW + timedelta(minutes=10))).approved
    assert "STOPPED_FOR_DAY" in e.on_trade_closed(Decimal(-500), OptType.PE, "stop_loss", NOW + timedelta(minutes=40))
    assert not e.check_entry(_ctx(now=NOW + timedelta(hours=2))).approved
    # next session clears day lock
    nxt = NOW + timedelta(days=1)
    assert e.check_entry(_ctx(now=nxt)).approved


def test_daily_loss_limit_and_mtm():
    e = _engine()
    assert e.update_mtm(Decimal(-5001), NOW) is True
    assert not e.check_entry(_ctx()).approved


def test_remaining_daily_budget_shrinks_size():
    e = _engine()
    e.on_trade_closed(Decimal(-3500), OptType.CE, "target", NOW)  # not a stop: no cooldown
    d = e.check_entry(_ctx(entry_price=Decimal(100), stop_price=Decimal(75)))
    assert not d.approved  # remaining Rs 1,500 < one-lot risk ~Rs 1,690


def test_emergency_requires_manual_reset_even_next_day():
    e = _engine()
    e.emergency_stop()
    assert not e.check_entry(_ctx(now=NOW + timedelta(days=1))).approved
    e.manual_reset(clear_emergency=True)
    assert e.check_entry(_ctx(now=NOW + timedelta(days=1))).approved


def test_state_roundtrip():
    e = _engine()
    e.on_trade_closed(Decimal(-100), OptType.CE, "stop_loss", NOW)
    s2 = RiskState.from_dict(e.state.to_dict())
    assert s2 == e.state


@settings(max_examples=200, deadline=None)
@given(entry=st.decimals(min_value=5, max_value=800, places=2), stop_frac=st.floats(0.3, 0.95),
       lot=st.sampled_from([15, 25, 30, 60, 65, 75, 120]), funds=st.integers(0, 500000),
       pnl=st.integers(-6000, 6000))
def test_sizing_invariants(entry, stop_frac, lot, funds, pnl):
    """Whatever the inputs: whole lots, within every limit, never above the fixed lot cap."""
    e = _engine()
    e.state.realised_pnl_today = Decimal(pnl)
    stop = (entry * Decimal(str(stop_frac))).quantize(Decimal("0.05"))
    d = e.check_entry(_ctx(entry_price=entry, stop_price=stop, lot_size=lot, available_funds=Decimal(funds)))
    if d.approved:
        cfg = e.cfg
        assert d.quantity % lot == 0 and 1 <= d.lots <= cfg.trades.max_lots_per_trade
        assert d.max_loss <= cfg.capital.max_loss_per_trade
        assert d.capital_required <= min(cfg.capital.max_capital_per_trade, funds)
        assert d.max_loss <= cfg.capital.max_daily_loss + min(pnl, 0)


# --- order manager ---------------------------------------------------------------
@pytest.fixture
def om_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/om.db")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    t = [0.0]

    def make(broker):
        return OrderManager(broker, sessions, FixedClock(NOW), "paper",
                            sleep=lambda s: t.__setitem__(0, t[0] + s), poll_interval=0.5,
                            monotonic=lambda: t[0])
    return make, sessions


APPROVED = RiskDecision(True, lots=1, quantity=65, max_loss=Decimal(1700), capital_required=Decimal(6500))
INST = opt(7, 24000, "CE")


def _rows(sessions):
    with sessions() as s:
        return list(s.scalars(select(Order)))


def test_entry_requires_approval(om_env):
    make, _ = om_env
    with pytest.raises(ExecutionError):
        make(ScriptedBroker()).submit_entry(INST, Decimal(100), RiskDecision(False), 10)


def test_entry_fill(om_env):
    make, sessions = om_env
    out = make(ScriptedBroker([(BrokerOrderStatus.OPEN, 0), (BrokerOrderStatus.FILLED, None)])).submit_entry(
        INST, Decimal(100), APPROVED, 10)
    assert out.status.value == "FILLED" and out.filled_quantity == 65
    assert _rows(sessions)[0].side == "BUY"


def test_partial_fill_then_timeout_cancels_and_keeps_partial(om_env):
    make, sessions = om_env
    b = ScriptedBroker([(BrokerOrderStatus.PARTIALLY_FILLED, 30)] * 100)
    out = make(b).submit_entry(INST, Decimal(100), RiskDecision(True, 2, 130, Decimal(1), Decimal(1)), 10)
    assert b.cancelled and out.status.value == "CANCELLED" and out.filled_quantity == 30


def test_rejection_recorded(om_env):
    make, sessions = om_env
    out = make(ScriptedBroker(place_error=OrderRejected("Insufficient funds"))).submit_entry(
        INST, Decimal(100), APPROVED, 10)
    assert out.status.value == "REJECTED" and "Insufficient" in _rows(sessions)[0].status_message


def test_timeout_but_order_exists_is_adopted_not_duplicated(om_env):
    make, sessions = om_env
    b = ScriptedBroker(place_error=NetworkError("read timeout"), accept_despite_error=True)
    out = make(b).submit_entry(INST, Decimal(100), APPROVED, 10)
    assert len(b.placed) == 1 and out.status.value == "FILLED"


def test_timeout_and_order_missing_stays_unknown(om_env):
    make, sessions = om_env
    b = ScriptedBroker(place_error=NetworkError("read timeout"))
    om = make(b)
    out = om.submit_entry(INST, Decimal(100), APPROVED, 10)
    assert out.status.value == "UNKNOWN" and len(b.placed) == 1
    assert om.reconcile_unresolved() == 1  # still unresolved -> engine blocks entries


def test_exit_never_exceeds_verified_position(om_env):
    make, _ = om_env
    b = ScriptedBroker()
    out = make(b).submit_exit(INST, 130, verified_long_qty=65, limit_price=Decimal(90), timeout_s=5)
    assert b.placed[0].quantity == 65 and b.placed[0].side.value == "SELL" and out.filled_quantity == 65
    with pytest.raises(ExecutionError):
        make(b).submit_exit(INST, 65, verified_long_qty=0, limit_price=Decimal(90), timeout_s=5)


# --- exits -----------------------------------------------------------------------
P = ExitParams()


def _plan(qty=65):
    return TradePlan.build(OptType.CE, Decimal(100), qty, 65, P, NOW, invalidation=24000.0)


def _mv(bid, minutes=1, spot=24050.0, **kw):
    return MarketView(now=NOW + timedelta(minutes=minutes), option_bid=Decimal(str(bid)), spot=spot, **kw)


SQ = time(15, 10)


def test_plan_levels():
    p = _plan()
    assert p.stop == Decimal(75) and p.target == Decimal(150)


def test_stop_target_invalidation_and_squareoff():
    assert evaluate(_plan(), _mv(74), P, SQ).reason == "stop_loss"
    assert evaluate(_plan(), _mv(151), P, SQ).reason == "target"
    assert evaluate(_plan(), _mv(105, spot=23990), P, SQ).reason == "underlying_invalidation"
    late = MarketView(now=NOW.replace(hour=15, minute=10), option_bid=Decimal(110), spot=24050)
    assert evaluate(_plan(), late, P, SQ).reason == "session_end"
    assert evaluate(_plan(), _mv(105, data_ok=False), P, SQ).reason == "data_unreliable"
    assert evaluate(_plan(), _mv(105), P, SQ, emergency=True).urgent


def test_breakeven_then_trailing():
    p = _plan()
    assert evaluate(p, _mv(126), P, SQ) is None and p.stop == Decimal(100)
    assert evaluate(p, _mv(99.95), P, SQ).reason == "trailing_stop"


def test_partial_only_with_two_lots():
    p = _plan(130)
    d = evaluate(p, _mv(138), P, SQ)
    assert d.reason == "partial_target" and d.quantity == 65
    p1 = _plan(65)
    assert evaluate(p1, _mv(138), P, SQ) is None


def test_time_stop_and_vwap_invalidation():
    assert evaluate(_plan(), _mv(105, minutes=31), P, SQ).reason == "time_stop"
    assert evaluate(_plan(), _mv(105, last_signal_close=24010.0, vwap=24020.0), P, SQ).reason == "signal_invalidated"


# --- reconciliation ----------------------------------------------------------------
def _bp(sym, q):
    return BrokerPosition("NFO", sym, 1, q, Decimal(100), "MIS")


def test_reconcile():
    r = reconcile({"A": 65, "B": 65}, [_bp("A", 65), _bp("B", 30), _bp("C", 65), _bp("D", -65)])
    assert r.matched == {"A": 65} and r.adjusted == {"B": (65, 30)}
    assert r.unknown_at_broker == {"C": 65} and r.shorts == {"D": -65} and r.critical
