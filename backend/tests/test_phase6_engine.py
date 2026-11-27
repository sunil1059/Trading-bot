from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.brokers.base import AuthError
from backend.brokers.paper import PaperBroker
from backend.core.calendar import MarketHours, TradingCalendar
from backend.core.clock import IST, FixedClock
from backend.core.config import RiskConfig, TradingMode
from backend.database.models import Base, Order, Position, Signal, Trade
from backend.database.session import make_engine, make_session_factory
from backend.execution.order_manager import OrderManager
from backend.market_data.health import DataHealth
from backend.notifications.service import Notifier
from backend.risk.charges import ChargeRates
from backend.services.engine import EngineError, TradingEngine
from backend.strategies.config import StrategyConfig
from backend.tests.fakes import FakeMarket
from backend.tests.test_phase4_analysis import _history

RATES = ChargeRates.from_yaml()


@pytest.fixture
def world(tmp_path):
    bars, today = _history()
    clock = FixedClock(datetime(today.year, today.month, today.day, 10, 40, 5, tzinfo=IST))
    db = make_engine(f"sqlite:///{tmp_path}/e.db")
    Base.metadata.create_all(db)
    sessions = make_session_factory(db)
    market = FakeMarket(clock, bars, today, spot=bars[[b.ts for b in bars].index(
        datetime(today.year, today.month, today.day, 10, 39, tzinfo=IST))].close)
    cal = TradingCalendar(MarketHours(), holidays={2030: set()})
    health = DataHealth(clock=clock, max_tick_age=timedelta(seconds=30))
    health.on_connect()

    def build(mode=TradingMode.PAPER, min_score=60):
        risk_cfg = RiskConfig.from_yaml().model_copy(update={"min_signal_score_for_entry": min_score})
        paper = PaperBroker(market, clock, Decimal(100000), RATES)
        t = [0.0]
        om = OrderManager(paper, sessions, clock, mode.value, sleep=lambda s: t.__setitem__(0, t[0] + s),
                          monotonic=lambda: t[0])
        eng = TradingEngine(mode=mode, risk_cfg=risk_cfg, strat=StrategyConfig(), rates=RATES, data=market,
                            broker=paper, sessions=sessions, clock=clock, calendar=cal, notifier=Notifier(clock),
                            health=health, order_manager=om)
        return eng, paper

    def tick():
        for tok in (900, 256265):
            health.record(tok, clock.now(), float(clock.now().timestamp()))

    return dict(build=build, clock=clock, market=market, sessions=sessions, tick=tick, today=today)


def _rows(sessions, model):
    with sessions() as s:
        return list(s.scalars(select(model)))


def test_paper_entry_then_stop_loss_exit(world):
    eng, paper = world["build"]()
    eng.start()
    world["tick"]()
    eng.step()
    sigs = _rows(world["sessions"], Signal)
    assert sigs and sigs[-1].status == "ACCEPTED", [(s.status, s.reason) for s in sigs]
    pos = _rows(world["sessions"], Position)
    assert len(pos) == 1 and pos[0].quantity == 65 and pos[0].status == "OPEN"
    assert all(o.side == "BUY" for o in _rows(world["sessions"], Order))
    # market drifts down 3 points every 5 seconds until the bot exits
    for _ in range(60):
        if _rows(world["sessions"], Trade):
            break
        world["clock"].advance(timedelta(seconds=5))
        world["market"].spot -= 3
        world["tick"]()
        eng.step()
    trades = _rows(world["sessions"], Trade)
    assert len(trades) == 1 and trades[0].net_pnl < 0
    assert trades[0].exit_reason in ("stop_loss", "underlying_invalidation")
    assert paper.get_positions()[0].quantity == 0
    assert eng.risk.state.trades_today == 1 and eng.risk.state.consecutive_losses == 1
    # loss stays within the per-trade limit (plus a little exit slippage tolerance)
    assert -trades[0].net_pnl <= Decimal(2500) * Decimal("1.1")


def test_signal_only_mode_places_no_orders(world):
    eng, paper = world["build"](TradingMode.SIGNAL_ONLY)
    eng.start()
    world["tick"]()
    eng.step()
    assert _rows(world["sessions"], Signal) and not _rows(world["sessions"], Order)


def test_stale_data_blocks_entry(world):
    eng, _ = world["build"]()
    eng.start()
    # no fresh ticks recorded -> data problems
    eng.step()
    sig = _rows(world["sessions"], Signal)[-1]
    assert sig.status == "REJECTED" and "data" in sig.reason


def test_restart_restores_position_and_does_not_reenter(world):
    eng, paper = world["build"]()
    eng.start()
    world["tick"]()
    eng.step()
    assert len(eng.open) == 1
    # simulated crash: new engine over the same DB and same (paper) broker
    eng2, _ = world["build"]()
    eng2.broker = paper
    eng2.om.broker = paper
    eng2.start()
    assert len(eng2.open) == 1
    world["clock"].advance(timedelta(seconds=30))
    world["tick"]()
    eng2.step()
    assert len(_rows(world["sessions"], Position)) == 1
    assert sum(1 for o in _rows(world["sessions"], Order) if o.purpose == "ENTRY") == 1


def test_emergency_stop_exits_and_locks(world):
    eng, paper = world["build"]()
    eng.start()
    world["tick"]()
    eng.step()
    res = eng.emergency_stop("tester")
    assert res["open_positions"] == 0 and paper.get_positions()[0].quantity == 0
    assert eng.risk.state.emergency_lock
    with pytest.raises(EngineError):
        eng.resume("tester")


def test_auth_failure_prevents_start(world):
    world["market"].auth_fail = True
    eng, _ = world["build"]()
    with pytest.raises(AuthError):
        eng.start()


def test_gap_through_stop_still_exits(world):
    eng, paper = world["build"]()
    eng.start()
    world["tick"]()
    eng.step()
    world["clock"].advance(timedelta(minutes=1))
    world["market"].spot -= 150
    world["tick"]()
    eng.step()
    assert len(_rows(world["sessions"], Trade)) == 1 and paper.get_positions()[0].quantity == 0
