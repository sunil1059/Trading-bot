# Phase 1 plan

The full Phase 1 architecture, broker comparison, strategy, risk limits, capital analysis,
testing framework, failure scenarios and roadmap live in the project doc:
https://claude.ai/artifact/3bGDBoCBJ3Bk8znQxPQGAn

Key decisions:
- Broker: Zerodha Kite Connect, paid Connect plan (data + WebSocket).
- Instrument: NIFTY weekly options only; ATM or 1 strike ITM; no expiry-day entries.
- Strategy: opening-range breakout filtered by VWAP and 15-min trend; score >= 75 to enter.
- Risk: see `config/risk.yaml`.
- LIVE only after out-of-sample backtest pass, 4+ weeks paper, and explicit user approval.
