"""Backtest the strategy on REAL historical 1-minute data.

  # 1) download near-month NIFTY futures candles from Kite (needs the Kite Connect plan;
  #    you log in in the browser and paste the request_token; nothing is saved but the CSV)
  python -m scripts.run_backtest fetch --days 55 --out data/nifty_fut_1m.csv

  # 2) run a backtest / walk-forward on that CSV
  python -m scripts.run_backtest run --csv data/nifty_fut_1m.csv --lot-size 65 --strike-step 50 \
      --expiry-weekday 1 --report data/backtest_report.json
  python -m scripts.run_backtest walkforward --csv data/nifty_fut_1m.csv --lot-size 65 \
      --strike-step 50 --expiry-weekday 1 --train-days 20 --test-days 5

Lot size, strike step and expiry weekday must match the period you test: read them from
the instrument master / NSE circulars. They are required arguments on purpose.

Option premiums are MODELLED (Black-Scholes); see docs/TESTING.md for what that means.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from backend.backtesting.data import load_csv, validate, write_csv
from backend.backtesting.engine import BacktestParams, by_regime, day_regimes, run_backtest, walk_forward
from backend.core.clock import IST
from backend.core.config import AppSettings, RiskConfig
from backend.risk.charges import ChargeRates
from backend.strategies.config import StrategyConfig

BANNER = ("NOTE: option prices are MODELLED (Black-Scholes + assumed spread/slippage). Results are an "
          "approximation, not evidence of profitability.")


def _fetch(a: argparse.Namespace) -> None:
    from backend.brokers.zerodha import ZerodhaBroker, exchange_request_token, login_url

    s = AppSettings()
    if s.kite_api_key is None or s.kite_api_secret is None:
        sys.exit("Set TB_KITE_API_KEY and TB_KITE_API_SECRET in .env first.")
    key = s.kite_api_key.get_secret_value()
    print("Open this URL, log in, and copy the request_token from the redirect URL:\n", login_url(key))
    req = getpass.getpass("request_token (hidden): ").strip()
    token = exchange_request_token(key, s.kite_api_secret.get_secret_value(), req)
    broker = ZerodhaBroker.from_credentials(key, token)
    now = datetime.now(IST)
    futs = sorted((i for i in broker.get_instruments("NFO") if i.name == a.underlying
                   and i.instrument_type == "FUT" and i.expiry and i.expiry >= now.date()), key=lambda i: i.expiry)
    if not futs:
        sys.exit(f"no {a.underlying} future found")
    fut = futs[0]
    bars, start = [], now - timedelta(days=a.days)
    while start < now:  # Kite limits minute data per request; fetch in 30-day chunks
        end = min(start + timedelta(days=30), now)
        bars += broker.get_historical_data(fut.instrument_token, start, end, "1m")
        start = end
    bars = sorted({b.ts: b for b in bars}.values(), key=lambda b: b.ts)
    bars = [b for b in bars if b.ts + timedelta(minutes=1) <= now]  # complete bars only
    validate(bars)
    write_csv(bars, Path(a.out))
    print(f"saved {len(bars)} bars of {fut.tradingsymbol} to {a.out}. "
          f"Only this contract's life is covered; older history needs older contracts.")


def _params(a: argparse.Namespace) -> BacktestParams:
    return BacktestParams(lot_size=a.lot_size, strike_step=a.strike_step, expiry_weekday=a.expiry_weekday,
                          iv=a.iv, slippage_pct=a.slippage, spread_pct=a.spread)


def _jsonable(o):
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(type(o))


def _run(a: argparse.Namespace) -> None:
    bars = load_csv(Path(a.csv))
    p = _params(a)
    r = run_backtest(bars, StrategyConfig.from_yaml(), RiskConfig.from_yaml(), ChargeRates.from_yaml(), p)
    r.by_regime = by_regime(r, day_regimes(bars), p.starting_capital)
    print(BANNER)
    print(json.dumps(r.metrics, indent=2, default=_jsonable))
    print(f"rejected signals: {r.rejected_signals}")
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(json.dumps({"note": BANNER, "params": r.params, "metrics": r.metrics,
                                              "by_regime": r.by_regime,
                                              "trades": [t.__dict__ for t in r.trades]}, indent=2,
                                             default=_jsonable))
        print("report written to", a.report)


def _walk(a: argparse.Namespace) -> None:
    bars = load_csv(Path(a.csv))
    grid = [{}, {"exits.target_r": 1.5}, {"exits.stop_premium_pct": 0.2}, {"signal.min_relative_volume": 2.0}]
    folds = walk_forward(bars, StrategyConfig.from_yaml(), RiskConfig.from_yaml(), ChargeRates.from_yaml(),
                         _params(a), grid, a.train_days, a.test_days)
    print(BANNER)
    total = 0.0
    for f in folds:
        m = f.test_result.metrics
        total += m["total_pnl"] or 0
        print(f"train {f.train[0]}..{f.train[1]} chose {f.chosen or 'defaults'} | test {f.test[0]}..{f.test[1]}: "
              f"{m['total_trades']} trades, pnl {m['total_pnl']:.0f}, pf {m['profit_factor']}")
    print(f"OUT-OF-SAMPLE total over {len(folds)} folds: {total:.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--underlying", default="NIFTY")
    f.add_argument("--days", type=int, default=55)
    f.add_argument("--out", default="data/nifty_fut_1m.csv")
    for name in ("run", "walkforward"):
        r = sub.add_parser(name)
        r.add_argument("--csv", required=True)
        r.add_argument("--lot-size", type=int, required=True)
        r.add_argument("--strike-step", type=int, required=True)
        r.add_argument("--expiry-weekday", type=int, required=True, help="0=Mon ... 4=Fri")
        r.add_argument("--iv", type=float, default=0.13)
        r.add_argument("--slippage", type=float, default=0.005)
        r.add_argument("--spread", type=float, default=0.004)
        if name == "run":
            r.add_argument("--report")
        else:
            r.add_argument("--train-days", type=int, default=20)
            r.add_argument("--test-days", type=int, default=5)
    a = ap.parse_args()
    {"fetch": _fetch, "run": _run, "walkforward": _walk}[a.cmd](a)


if __name__ == "__main__":
    main()
