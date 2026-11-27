# Trading Bot — long-only Indian index options

A disciplined, test-first system that **only buys** CE/PE options on NSE index options (NIFTY first) through Zerodha Kite Connect.

> **No profitability claim.** The strategy is unvalidated until walk-forward backtests and at least 4 weeks of paper trading say otherwise.
> **LIVE trading is disabled by default** and stays locked until it is explicitly enabled after all safety gates pass.

## Safety invariants

- Opening orders are always BUY; closing orders are always SELL and never exceed the verified broker position (enforced in code *and* by database constraints).
- No futures, no naked selling, no averaging down, no martingale, no size-up after wins or losses.
- Risk limits are validated against hard ceilings in `backend/core/config.py`; a looser config makes the bot refuse to start.
- Unknown market calendar, stale data or broker doubt ⇒ no new entries.
- Contract specs (lot size, expiry, strikes) come from the broker instrument master daily; nothing is hardcoded.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Architecture, roadmap, risk analysis ([plan](docs/PLAN.md)) | Done |
| 2 | Project setup, config, database, logging | Done |
| 3 | Zerodha broker layer, instruments, live tick feed, candles, data health | Done |
| 4 | Indicators, Greeks, ORB strategy, contract selection, 0–100 score | Done |
| 5 | Risk engine, idempotent order manager, exits, reconciliation | Done |
| 6 | Paper broker, trading engine, alerts, dashboard | Done |
| 7 | Backtester and walk-forward ([testing](docs/TESTING.md)) | Done |
| 8 | Security, risk and failure audit ([audit](docs/AUDIT.md)) | Done |
| 9 | 4+ weeks of PAPER trading on real data | **Your turn** |
| 10 | LIVE | Locked in this build |

## Before the first run

1. **Fill the NSE holiday list** in `config/nse_calendar.yaml` from the official NSE circular.
   Until a year is filled, the bot refuses to trade in that year (by design).
2. Subscribe to the **Kite Connect** plan (the free Personal plan has no market data).
   In the Kite developer console set the redirect URL to
   `http://127.0.0.1:8000/auth/kite/callback`.
3. Create `.env` from `.env.example` and fill it **on your own machine**:
   `TB_KITE_API_KEY`, `TB_KITE_API_SECRET`, `TB_DASHBOARD_SECRET_KEY` (any long random string),
   `TB_DASHBOARD_PASSWORD_HASH` (from `python -m scripts.hash_password`), optional Telegram/email.
   Never paste these into chats, issues or commits.

## Run paper trading

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
(cd frontend && npm ci && npm run build)   # dashboard
alembic upgrade head
python -m scripts.check_config
python -m scripts.run_server               # http://127.0.0.1:8000
```

Each trading morning: open the dashboard, sign in, press **Log in to Kite** and complete
Zerodha's login (2FA). Kite tokens last one trading day, so this is a daily step.
The engine then runs in PAPER mode: real live prices, virtual money.

Notes:
- The NIFTY index has no volume, so analysis uses the **near-month NIFTY futures** candles as
  data only. The bot never trades futures; strikes come from the NIFTY 50 spot.
- LIVE mode is **locked** in this build: the server refuses to start in LIVE.
- Backtesting: see [docs/TESTING.md](docs/TESTING.md). Option prices there are modelled.

## Tests

```bash
pytest -q && ruff check backend scripts
```

## With Docker (PostgreSQL)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD and the values above
docker compose up --build     # dashboard on http://127.0.0.1:8000
```

## Configuration

- `config/risk.yaml` — every risk/trading limit, documented inline. Defaults: ₹1,00,000 paper capital, ₹2,500 max loss per trade, ₹5,000 per day, 3 trades/day, 1 open position, entries 09:30–14:30 IST, square-off 15:10.
- `config/nse_calendar.yaml` — NSE holidays, filled **only** from the official NSE circular. Missing years block trading.
- `.env` — secrets and deployment settings (`TB_` prefix). See `.env.example`.

## Layout

```
backend/
  core/         config, IST clock, NSE calendar
  database/     SQLAlchemy models, session, Alembic migrations
  logging/      JSON logging with secret redaction
  brokers/      Zerodha (Kite), paper broker, Angel One / Upstox stubs
  market_data/  instruments, tick feed, candles, data health
  indicators/ options/ strategies/   analysis, Greeks, contract selection, ORB strategy
  risk/ execution/ positions/        risk engine, order manager, exits, reconciliation
  services/     trading engine, runtime, metrics
  backtesting/  event-driven backtester, walk-forward
  notifications/ api/                Telegram/email alerts, dashboard API
  tests/
config/  docker/  docs/  scripts/  frontend/ (Next.js dashboard)
```
