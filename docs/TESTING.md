# Testing and validation

The bot is judged in this order: automated tests, then backtest, then walk-forward
(out-of-sample), then at least 4 weeks of PAPER trading. LIVE stays locked in this build.

## 1. Automated tests

```bash
.venv/bin/python -m pytest -q          # all tests
.venv/bin/ruff check .                 # lint
.venv/bin/pip-audit -r requirements.txt
cd frontend && npm ci && npm run build && npm audit
```

What the tests cover (by file):

| File | Covers |
|---|---|
| `test_config.py`, `test_calendar.py`, `test_logging.py`, `test_database.py` | config ceilings, LIVE disabled by default, IST calendar (unknown years refused), secret redaction, DB long-only constraints, timezone-aware timestamps |
| `test_phase3_broker_data.py` | Kite error mapping (429, token expiry, network), no retry on order placement, MARKET buy refused, tick-to-candle aggregation, out-of-order/duplicate/future ticks, stale data |
| `test_phase4_analysis.py` | indicators, Greeks/IV, ORB analysis, **no look-ahead** (property test: changing future bars never changes the past), contract selection (ATM/ITM only, liquidity, spread, stale quote) |
| `test_phase5_risk_execution.py` | every risk limit, whole-lot sizing invariants, idempotent orders (timeout leads to UNKNOWN then lookup by tag, never a blind retry), duplicate orders, partial fills, exits, reconciliation (unknown or short positions trigger a lock) |
| `test_phase6_engine.py`, `test_phase6_api.py` | end-to-end paper day, restart recovery, emergency stop, gap through stop, stale data, signal-only mode, dashboard auth, CSRF, confirmation phrases, LIVE refused |
| `test_phase7_backtest.py` | backtest rules (entry window, square-off, 1 position, trade cap, ATM/ITM only, never same-day expiry), determinism, **no look-ahead** (future data cannot change past trades), costs reduce P&L, regimes, walk-forward windows do not overlap, CSV validation |
| `test_phase8_failures.py` | failure scenarios: API down, token expiry mid-session, rate limit, order rejected, duplicate submission, crash and restart with an open position, broker shows a position the bot does not know |

`backend/tests/synthetic.py` makes **synthetic** prices for tests only. They exercise the
code paths; they say nothing about whether the strategy makes money.

## 2. Backtest

```bash
python -m scripts.run_backtest fetch --days 55 --out data/nifty_fut_1m.csv
python -m scripts.run_backtest run --csv data/nifty_fut_1m.csv \
    --lot-size 65 --strike-step 50 --expiry-weekday 1 --report data/backtest_report.json
```

How it simulates (see `backend/backtesting/engine.py`):

- It uses the **same** analysis, risk engine and exit rules as paper trading.
- A signal on a 5-minute close fills at the **next minute's open**. If the modelled ask plus
  slippage is above the limit price, the order does not fill.
- Stops are checked at each minute's worst price. If stop and target are both touched in one
  minute, the stop is assumed to fill first.
- Exits pay the modelled bid minus slippage. A stop never fills better than the stop price.
- Every trade pays full Zerodha charges (brokerage, STT, exchange, SEBI, stamp, GST) from
  `config/charges.yaml`.

**Main limitation:** option premiums are **modelled** with Black-Scholes from the futures
price and an IV assumption. Real premiums also move with IV, skew and order-book depth.
Kite does not provide history for expired option contracts. Treat backtest numbers as a
sanity check, not proof. The bot records real option quotes while paper trading, and those
results are the evidence that counts.

Other limits:

- Futures prices are used as the underlying. The small basis to spot is ignored.
- Expiry-day holiday shifts are not modelled.
- `fetch` covers only the current near-month contract's life. Older history needs older
  contracts or another data source.

## 3. Walk-forward (out-of-sample)

```bash
python -m scripts.run_backtest walkforward --csv data/nifty_fut_1m.csv \
    --lot-size 65 --strike-step 50 --expiry-weekday 1 --train-days 20 --test-days 5
```

For each fold it picks the best of a **small** parameter grid on the training window
(minimum trade count required), then runs that choice untouched on the next test window.
Only the test-window total counts. Keep the grid small: a large grid finds patterns that
are just noise.

## 4. Paper trading gate (before anyone even considers LIVE)

Run PAPER for at least 4 weeks (about 20 sessions) and check:

- No unhandled errors, no unreconciled orders, no duplicate orders.
- Every risk limit fired correctly when hit (see the activity log).
- At least 30 closed trades. Profit factor above 1.2 **after charges**. Max drawdown inside
  the limits you are willing to lose for real.
- Paper fills compared with the real quotes at those times look realistic.

If any check fails, stay in PAPER. LIVE needs a separate, deliberate code change and review;
this build refuses to start in LIVE.
