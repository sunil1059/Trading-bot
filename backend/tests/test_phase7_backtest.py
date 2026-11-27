from dataclasses import replace
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from backend.backtesting.data import BadData, load_csv, write_csv
from backend.backtesting.engine import BacktestParams, by_regime, day_regimes, run_backtest, walk_forward
from backend.core.config import RiskConfig
from backend.risk.charges import ChargeRates
from backend.strategies.config import StrategyConfig
from backend.tests.synthetic import synthetic_sessions, trading_days

RATES = ChargeRates.from_yaml()
STRAT = StrategyConfig.from_yaml()
RISK = RiskConfig.from_yaml().model_copy(update={"min_signal_score_for_entry": 50})
P = BacktestParams(lot_size=65, strike_step=50, expiry_weekday=1)
DAYS = trading_days(date(2030, 1, 7), 14)
BARS = synthetic_sessions(DAYS[0], len(DAYS))
START = DAYS[6]


@pytest.fixture(scope="module")
def base():
    return run_backtest(BARS, STRAT, RISK, RATES, P, start=START)


def test_trades_respect_rules(base):
    assert base.trades, "synthetic data should produce some trades"
    assert base.modelled_prices
    sq = RISK.session.square_off
    for t in base.trades:
        assert RISK.session.first_entry <= t.entry_time.time() <= time(14, 31)
        assert t.exit_time.time() <= sq
        assert t.quantity % P.lot_size == 0 and t.quantity // P.lot_size <= RISK.trades.max_lots_per_trade
        assert t.charges > 0
        assert t.net == (t.gross - t.charges).quantize(Decimal("0.01"))
        assert t.entry_price % Decimal("0.05") == 0
        assert t.strike % P.strike_step == 0
        assert t.expiry > t.entry_time.date()  # never same-day expiry
    per_day = {}
    for t in base.trades:
        per_day[t.entry_time.date()] = per_day.get(t.entry_time.date(), 0) + 1
    assert max(per_day.values()) <= RISK.trades.max_trades_per_day
    # never overlapping positions (max 1 open)
    for a, b in zip(base.trades, base.trades[1:], strict=False):
        assert b.entry_time >= a.exit_time


def test_strike_is_atm_or_itm_never_otm(base):
    for t in base.trades:
        spot = next(b.open for b in BARS if b.ts == t.entry_time)
        if t.direction == "CE":
            assert t.strike <= round(spot / 50) * 50
        else:
            assert t.strike >= round(spot / 50) * 50


def test_deterministic(base):
    again = run_backtest(BARS, STRAT, RISK, RATES, P, start=START)
    assert [(t.entry_time, t.net) for t in again.trades] == [(t.entry_time, t.net) for t in base.trades]


def test_no_lookahead_future_data_does_not_change_past_trades(base):
    cut = DAYS[10]
    truncated = [b for b in BARS if b.ts.date() <= cut]
    r = run_backtest(truncated, STRAT, RISK, RATES, P, start=START)
    full_past = [(t.entry_time, t.exit_time, t.net) for t in base.trades if t.exit_time.date() <= cut]
    assert [(t.entry_time, t.exit_time, t.net) for t in r.trades] == full_past


def test_costs_reduce_pnl(base):
    costly = run_backtest(BARS, STRAT, RISK, RATES, replace(P, spread_pct=0.008), start=START)
    assert costly.trades
    assert costly.metrics["total_pnl"] < base.metrics["total_pnl"]


def test_slippage_beyond_limit_allowance_means_no_fill():
    r = run_backtest(BARS, STRAT, RISK, RATES, replace(P, slippage_pct=0.05), start=START)
    assert r.trades == []


def test_stop_never_fills_better_than_stop(base):
    stops = [t for t in base.trades if t.exit_reason == "stop_loss"]
    for t in stops:
        assert t.exit_price < t.entry_price


def test_regimes(base):
    reg = day_regimes(BARS)
    assert set(reg) == set(DAYS)
    assert all({"bull", "bear"} & tags and {"high_vol", "low_vol"} & tags for tags in reg.values())
    rep = by_regime(base, reg, P.starting_capital)
    assert sum(rep[k]["total_trades"] for k in ("bull", "bear") if k in rep) == len(base.trades)


def test_walk_forward_folds_do_not_overlap():
    grid = [{}, {"exits.target_r": 1.5}]
    folds = walk_forward(BARS, STRAT, RISK, RATES, P, grid, train_days=4, test_days=2, min_train_trades=1)
    assert len(folds) == 2
    for f in folds:
        assert f.train[1] < f.test[0]
        assert all(f.test[0] <= t.entry_time.date() <= f.test[1] for t in f.test_result.trades)
    assert folds[0].test[1] < folds[1].test[0]


def test_csv_roundtrip_and_validation(tmp_path: Path):
    p = tmp_path / "b.csv"
    write_csv(BARS[:400], p)
    assert load_csv(p) == BARS[:400]
    write_csv([BARS[1], BARS[0]], p)
    with pytest.raises(BadData, match="out-of-order"):
        load_csv(p)
    bad = replace(BARS[0], high=BARS[0].low - 1)
    write_csv([bad], p)
    with pytest.raises(BadData, match="OHLC"):
        load_csv(p)
    write_csv([replace(b, volume=0) for b in BARS[:10]], p)
    with pytest.raises(BadData, match="no volume"):
        load_csv(p)
