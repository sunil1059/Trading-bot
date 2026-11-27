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
| 2 | Project setup, config, database, logging | In review |
| 3 | Broker layer + Kite market data | Next |
| 4–10 | Signals, risk/execution, paper + dashboard, backtests, audits, paper run, (optional) LIVE | Planned |

## Quick start (local, SQLite)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill locally; never commit
alembic upgrade head
python -m scripts.check_config
pytest -q
```

## With Docker (PostgreSQL)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD
docker compose up --build
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
  brokers/ market_data/ indicators/ strategies/ signals/ options/
  risk/ execution/ positions/ backtesting/ notifications/ api/   (later phases)
  tests/
config/  docker/  docs/  scripts/  frontend/ (Phase 6)
```
