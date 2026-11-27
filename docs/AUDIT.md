# Phase 8 audit (2026-11-27)

Scope: security, risk controls, order safety, API failure handling and emergency shutdown
of the PAPER / SIGNAL_ONLY build. LIVE is locked in this build: `Runtime` refuses to start
in LIVE mode.

## Findings fixed during the audit

| # | Area | Finding | Fix | Test |
|---|---|---|---|---|
| 1 | Reconciliation | A position closed outside the bot crashed reconciliation (`dictionary changed size during iteration`). | Iterate over a copy. | `test_position_closed_outside_bot_is_adopted` |
| 2 | Risk | That same position was booked with exit price 0. Only charges were counted, so the real loss never reached the daily or weekly limits. | The missing quantity is booked at the stop price (worst planned case) and a critical `EXIT_PRICE_UNKNOWN` event asks you to check the broker. | `test_position_closed_outside_bot_books_conservative_loss` |
| 3 | Auth | The dashboard session cookie was `SameSite=strict`. Browsers do not send it when Zerodha redirects back, so the daily Kite login would have failed. | `SameSite=lax`. All state-changing endpoints are POST with a CSRF token, and that is unchanged. | `test_kite_login_flow_requires_matching_state` |
| 4 | Auth | The Kite callback did not prove that this dashboard started the login. | A one-time `state` value goes through Kite's documented `redirect_params` and must match the session. It is single use. | same |
| 5 | Deploy | The Docker image did not run the server, and inside the container the API bound to 127.0.0.1, so it was unreachable. | Multi-stage image (dashboard + API). Compose binds 0.0.0.0 inside the container and publishes only on host `127.0.0.1:8000`. | manual |
| 6 | Deps | `requirements.txt` was missing kiteconnect, fastapi, uvicorn, httpx and itsdangerous. | Added, pinned. | CI |

## Accepted risk

- **autobahn 19.11.2** (PYSEC-2020-25, PYSEC-2026-4027). kiteconnect 5.2.2, the latest
  release, hard-pins this version. Both advisories affect autobahn as a WebSocket **server**
  (redirect header injection; compressed-size limit bypass). The bot only uses it as a
  **client** to Kite's ticker. These two IDs are ignored in CI by name. Revisit when
  kiteconnect releases a new version.

## Checklist

**Secrets**

- [x] No credentials in source or config. Everything comes from `.env` (gitignored) as `SecretStr`.
- [x] Secrets are redacted from logs, including Kite access tokens, which are registered at login.
- [x] Kite access tokens are kept in memory only, never written to disk or the DB.

**Dashboard**

- [x] Binds to localhost by default.
- [x] Requires a password (scrypt hash); wrong passwords are throttled.
- [x] Session cookie is HttpOnly and SameSite=lax (Secure when `TB_DASHBOARD_COOKIE_SECURE=true`).
- [x] CSRF tokens on all controls; typed confirmation for resume, close-all and reset.
- [x] Security headers are set (CSP, X-Frame-Options DENY, nosniff, no-referrer).

**Order safety**

- [x] Only BUY opens and only SELL closes. Enforced in the order model, the order manager,
  the paper broker and a DB check constraint.
- [x] A SELL never exceeds the verified long quantity.
- [x] Idempotent submission: the client id is persisted before the call and sent as the
  Kite tag. A timeout marks the order UNKNOWN and it is looked up by tag. Placements are
  never retried blindly.
- [x] Orders stay under 10 per second; HTTP 429 is mapped to a rate-limit error.
- [x] Entries are LIMIT only. MARKET exits use Kite market protection (-1, auto).

**Risk**

- [x] The risk engine cannot be bypassed: the order manager needs an approved decision.
- [x] Config is validated against hard ceilings in code.
- [x] Daily and weekly loss limits include open MTM.
- [x] Pause after consecutive losses, stop for the day, and re-entry cooldown.
- [x] The emergency lock persists across restarts and needs a manual, confirmed reset.

**Data and calendar**

- [x] Stale ticks, frozen candles, a lost connection or a lost login block entries.
- [x] When an open position can't be monitored, the bot exits.
- [x] Years missing from the holiday calendar block trading.

**Failure scenarios** (`test_phase8_failures.py`, `test_phase6_engine.py`, `test_phase5_*`)

- [x] Quote API down with a position open.
- [x] Token expiry mid-session.
- [x] Broker rejects an order.
- [x] Unknown or short position at the broker leads to an emergency lock.
- [x] A position closed outside the bot.
- [x] Crash and restart: no duplicate entry; the emergency lock survives.
- [x] A gap through the stop.
- [x] Rate limit.
- [x] Duplicate client ids.
- [x] Partial fills.

**Frontend**

- [x] `npm audit`: 0 vulnerabilities (next 16.4.6).

## Open items (need you or real data)

1. Fill `config/nse_calendar.yaml` from the official NSE holiday circular.
2. Check that the lot size and expiry weekday in the instrument master match NSE's current
   circulars on the first paper day. The bot reads them; it does not assume them.
3. Paper run (Phase 9): check that paper fills look realistic against real quotes.
4. Before any LIVE work: static IP (SEBI/Zerodha requirement since 1 Apr 2025), a separate
   review, and enabling LIVE deliberately in code. Out of scope for this build.
