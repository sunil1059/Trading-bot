"""Option contract selection: conservative ATM / slightly-ITM, liquidity-gated.

Never selects OTM strikes, illiquid contracts, wide spreads, stale or abnormal quotes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_CEILING, Decimal

from backend.market_data.instruments import InstrumentError, OptionUniverse
from backend.models.domain import Instrument, OptType, Quote
from backend.options.greeks import bs_delta, implied_vol
from backend.strategies.config import ContractParams

MARKET_CLOSE = time(15, 30)


@dataclass
class Candidate:
    instrument: Instrument
    quote: Quote | None
    rejections: list[str] = field(default_factory=list)
    delta: float | None = None
    iv: float | None = None


@dataclass
class Selection:
    instrument: Instrument | None
    quote: Quote | None
    liquidity_points: int
    reasons: list[str]
    delta: float | None = None
    iv: float | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.instrument is not None


def pick_expiry(universe: OptionUniverse, today: date) -> date | None:
    """Nearest expiry strictly after today (no same-day expiry trades)."""
    future = [e for e in universe.expiries() if e > today]
    return future[0] if future else None


def candidate_strikes(universe: OptionUniverse, expiry: date, spot: float, opt: OptType,
                      itm_steps: int) -> list[Decimal]:
    atm = universe.atm_strike(expiry, spot)
    step = universe.strike_step(expiry)
    strikes = set(universe.strikes(expiry))
    out = [atm]
    for i in range(1, itm_steps + 1):
        k = atm - step * i if opt is OptType.CE else atm + step * i  # ITM: below spot for CE, above for PE
        if k in strikes:
            out.append(k)
    return out


def years_to_expiry(now: datetime, expiry: date) -> float:
    close = datetime.combine(expiry, MARKET_CLOSE, tzinfo=now.tzinfo)
    return max((close - now).total_seconds(), 0) / (365 * 24 * 3600)


def check_quote(c: Candidate, now: datetime, p: ContractParams) -> None:
    q = c.quote
    if q is None:
        c.rejections.append("no quote")
        return
    bid, ask, mid = q.best_bid, q.best_ask, q.mid
    if bid is None or ask is None or mid is None:
        c.rejections.append("empty or crossed order book")
        return
    if (now - q.ts).total_seconds() > p.max_quote_age_seconds:
        c.rejections.append("stale quote")
    if q.last_trade_time is None or (now - q.last_trade_time).total_seconds() > p.max_last_trade_age_seconds:
        c.rejections.append("no recent trade")
    spread = float(ask - bid)
    if spread > p.max_spread_abs or spread > p.max_spread_pct * float(mid):
        c.rejections.append(f"spread {spread:.2f} too wide")
    lot = c.instrument.lot_size
    if q.bids[0].quantity < lot or q.asks[0].quantity < lot:
        c.rejections.append("top-of-book size below one lot")
    if q.volume < p.min_volume:
        c.rejections.append(f"volume {q.volume} below {p.min_volume}")
    if q.oi is None or q.oi < p.min_oi:
        c.rejections.append(f"OI {q.oi} below {p.min_oi}")
    ltp = float(q.last_price)
    if ltp < float(bid) * (1 - p.max_ltp_outside_book_pct) or ltp > float(ask) * (1 + p.max_ltp_outside_book_pct):
        c.rejections.append("last price far outside bid/ask (abnormal)")


def select_contract(universe: OptionUniverse, opt: OptType, spot: float, now: datetime,
                    quotes: dict[int, Quote], p: ContractParams) -> Selection:
    expiry = pick_expiry(universe, now.date())
    if expiry is None:
        return Selection(None, None, 0, ["no future expiry available"])
    try:
        strikes = candidate_strikes(universe, expiry, spot, opt, p.max_itm_steps)
        cands = [Candidate(universe.contract(expiry, k, opt), None) for k in strikes]
    except InstrumentError as e:
        return Selection(None, None, 0, [str(e)])
    t = years_to_expiry(now, expiry)
    for c in cands:
        c.quote = quotes.get(c.instrument.instrument_token)
        check_quote(c, now, p)
        if c.quote is not None and c.quote.mid is not None and c.instrument.strike is not None:
            c.iv = implied_vol(opt, float(c.quote.mid), spot, float(c.instrument.strike), t, p.risk_free_rate)
            if c.iv is not None:
                c.delta = abs(bs_delta(opt, spot, float(c.instrument.strike), t, p.risk_free_rate, c.iv))
                lo, hi = p.delta_band
                if not lo <= c.delta <= hi:
                    c.rejections.append(f"delta {c.delta:.2f} outside {lo}-{hi}")

    atm = cands[0]
    order = cands
    if atm.quote is not None and atm.quote.mid is not None and float(atm.quote.mid) < p.min_atm_premium:
        order = cands[1:] + [atm]  # cheap ATM: prefer one strike ITM first
    for c in order:
        if not c.rejections:
            q = c.quote
            assert q is not None and q.mid is not None and q.spread is not None
            tight = float(q.spread) <= 0.5 * p.max_spread_pct * float(q.mid)
            return Selection(c.instrument, q, 10 if tight else 6,
                             [f"selected {c.instrument.tradingsymbol}"], delta=c.delta, iv=c.iv, candidates=cands)
    reasons = [f"{c.instrument.tradingsymbol}: {', '.join(c.rejections)}" for c in cands]
    return Selection(None, None, 0, reasons, candidates=cands)


def round_to_tick(price: Decimal, tick: Decimal, up: bool) -> Decimal:
    q = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else "ROUND_FLOOR")
    return (q * tick).quantize(tick)
