"""Event-driven backtester using the SAME analysis, risk and exit code as the live engine.

Time model (prevents look-ahead):
- The loop walks 1-minute bars. At minute t the backtester knows only bars that ENDED at
  or before t.
- A signal evaluated at the close of a 5-minute bar is filled at the NEXT minute's open
  (plus spread and slippage), modelling order latency.
- Stops are checked against each minute's adverse extreme; if a stop and a target are both
  touched inside one minute, the stop is assumed to fill first (conservative).

Option prices (IMPORTANT LIMITATION):
- Option premiums are MODELLED with Black-Scholes from the underlying and an IV
  assumption (constant, or ``iv_fn`` e.g. from India VIX history), with a modelled
  spread and slippage. Real option prices also move with IV changes, skew and order-book
  depth, so results are APPROXIMATIONS and every report says so.
- Kite does not keep history for expired option contracts, so the better source is the
  option quotes the bot RECORDS itself during paper trading. Replaying recorded option
  data is a planned extension; until then judge the strategy on paper-trading results.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from backend.core.clock import IST
from backend.core.config import RiskConfig
from backend.market_data.candles import resample
from backend.models.domain import Bar, OptType
from backend.options.greeks import bs_delta, bs_price
from backend.options.selection import round_to_tick
from backend.positions.exits import MarketView, TradePlan, evaluate
from backend.risk.charges import ChargeRates, leg_charges
from backend.risk.engine import EntryContext, RiskEngine, RiskState, iso_week
from backend.services.metrics import TradeResult, compute_metrics
from backend.strategies.config import StrategyConfig
from backend.strategies.orb import analyse

ANALYSIS_WINDOW_BARS = 6 * 375  # same trailing window as the live engine
TICK = Decimal("0.05")


@dataclass(frozen=True)
class BacktestParams:
    lot_size: int  # REQUIRED: from the instrument master for the period tested
    strike_step: int  # REQUIRED: from the instrument master
    expiry_weekday: int  # REQUIRED: 0=Mon ... 6=Sun, for the period tested
    iv: float = 0.13  # used only when option prices are modelled
    iv_fn: Callable[[datetime], float] | None = None
    spread_pct: float = 0.004  # modelled half-spread x2 as % of price
    min_spread: float = 0.10
    slippage_pct: float = 0.005
    risk_free_rate: float = 0.065
    starting_capital: float = 100_000


@dataclass
class BtTrade:
    entry_time: datetime
    exit_time: datetime
    direction: str
    strike: int
    expiry: date
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross: Decimal
    charges: Decimal
    net: Decimal
    exit_reason: str
    score: int


@dataclass
class BacktestResult:
    trades: list[BtTrade]
    metrics: dict
    rejected_signals: int
    modelled_prices: bool
    params: dict = field(default_factory=dict)
    by_regime: dict = field(default_factory=dict)


def _expiry_for(d: date, weekday: int) -> date:
    """Nearest expiry strictly after ``d`` on the given weekday (holiday shifts not modelled)."""
    delta = (weekday - d.weekday()) % 7
    return d + timedelta(days=delta or 7)


def _is_signal_close(now_end: datetime, sig_min: int) -> bool:
    mins = now_end.hour * 60 + now_end.minute - (9 * 60 + 15)
    return mins > 0 and mins % sig_min == 0


def _years(now: datetime, expiry: date) -> float:
    close = datetime.combine(expiry, time(15, 30), tzinfo=IST)
    return max((close - now).total_seconds(), 60) / (365 * 24 * 3600)


class OptionPricer:
    def __init__(self, p: BacktestParams) -> None:
        self.p = p

    def mid(self, opt: OptType, spot: float, strike: int, now: datetime, expiry: date) -> float:
        iv = self.p.iv_fn(now) if self.p.iv_fn else self.p.iv
        return max(bs_price(opt, spot, strike, _years(now, expiry), self.p.risk_free_rate, iv), 0.05)

    def delta(self, opt: OptType, spot: float, strike: int, now: datetime, expiry: date) -> float:
        iv = self.p.iv_fn(now) if self.p.iv_fn else self.p.iv
        return abs(bs_delta(opt, spot, strike, _years(now, expiry), self.p.risk_free_rate, iv))

    def bid_ask(self, mid: float) -> tuple[Decimal, Decimal]:
        half = max(self.p.min_spread, mid * self.p.spread_pct) / 2
        bid = max(round_to_tick(Decimal(str(mid - half)), TICK, up=False), TICK)
        ask = round_to_tick(Decimal(str(mid + half)), TICK, up=True)
        return bid, ask


def run_backtest(bars: list[Bar], strat: StrategyConfig, risk_cfg: RiskConfig, rates: ChargeRates,
                 p: BacktestParams, start: date | None = None, end: date | None = None) -> BacktestResult:
    """``bars``: 1-minute bars of the analysis series (with volume), oldest first, several
    sessions of warm-up before ``start``."""
    pricer = OptionPricer(p)
    first_day = start or bars[0].ts.date()
    risk = RiskEngine(risk_cfg, rates, RiskState(day=first_day, week=iso_week(first_day)))
    sq = risk_cfg.session.square_off
    trades: list[BtTrade] = []
    rejected = 0
    pending: dict | None = None  # entry decided at bar close, filled next minute
    open_t: dict | None = None
    sig_min = strat.signal.signal_minutes

    for i, bar in enumerate(bars):
        now_end = bar.ts + timedelta(minutes=1)  # this bar is complete at now_end
        d = bar.ts.date()
        if (start and d < start) or (end and d > end):
            continue
        risk.roll(now_end)
        spot_open, spot_close = bar.open, bar.close

        # 1) fill a pending entry at this bar's open
        if pending is not None and open_t is None:
            mid = pricer.mid(pending["opt"], spot_open, pending["strike"], bar.ts, pending["expiry"])
            _, ask = pricer.bid_ask(mid)
            fill = round_to_tick(ask * (1 + Decimal(str(p.slippage_pct))), TICK, up=True)
            if fill <= pending["limit"]:
                plan = TradePlan.build(pending["opt"], fill, pending["qty"], p.lot_size, strat.exits, bar.ts,
                                       pending["invalidation"], pending["under_stop"])
                open_t = dict(plan=plan, strike=pending["strike"], expiry=pending["expiry"], score=pending["score"],
                              entry_time=bar.ts, entry=fill, qty=pending["qty"], exit_value=Decimal(0), exit_qty=0)
                risk.on_entry_filled(bar.ts)
            pending = None

        # 2) manage the open position on this bar
        if open_t is not None:
            plan: TradePlan = open_t["plan"]
            bull = plan.direction is OptType.CE
            adverse_spot = bar.low if bull else bar.high
            for spot_pt, ts in ((adverse_spot, bar.ts + timedelta(seconds=30)), (spot_close, now_end)):
                if plan.quantity <= 0:
                    break
                mid = pricer.mid(plan.direction, spot_pt, open_t["strike"], ts, open_t["expiry"])
                bid, _ = pricer.bid_ask(mid)
                sig_close = None
                if ts == now_end and _is_signal_close(now_end, sig_min):
                    sb = resample(bars[max(0, i - sig_min * 3):i + 1], sig_min, now=now_end)
                    if sb and sb[-1].ts.date() == d:
                        sig_close = sb[-1].close
                mv = MarketView(now=ts, option_bid=bid, spot=spot_pt, last_signal_close=sig_close, vwap=None)
                dec = evaluate(plan, mv, strat.exits, sq,
                               risk_lock=bool(risk.state.day_lock_reason or risk.state.week_lock_reason),
                               emergency_policy=risk_cfg.emergency_exit_policy)
                if dec is not None:
                    px = max(round_to_tick(bid * (1 - Decimal(str(p.slippage_pct))), TICK, up=False), TICK)
                    if dec.reason in ("stop_loss", "trailing_stop"):
                        px = min(px, plan.stop)  # stop fills no better than the stop price
                    qty = min(dec.quantity, plan.quantity)
                    open_t["exit_value"] += px * qty
                    open_t["exit_qty"] += qty
                    plan.quantity -= qty
                    open_t["last_reason"] = dec.reason
                    open_t["exit_time"] = ts
            if plan.quantity <= 0:
                q = open_t["qty"]
                avg_exit = open_t["exit_value"] / open_t["exit_qty"]
                gross = (avg_exit - open_t["entry"]) * q
                charges = leg_charges(rates, open_t["entry"], q, True) + leg_charges(rates, avg_exit, q, False)
                net = gross - charges
                trades.append(BtTrade(open_t["entry_time"], open_t["exit_time"], plan.direction.value,
                                      open_t["strike"], open_t["expiry"], q, open_t["entry"],
                                      avg_exit.quantize(Decimal("0.01")), gross.quantize(Decimal("0.01")), charges,
                                      net.quantize(Decimal("0.01")), open_t["last_reason"], open_t["score"]))
                risk.on_trade_closed(net, plan.direction, open_t["last_reason"], open_t["exit_time"])
                open_t = None

        # 3) new signal only at the close of a signal bar, when flat
        if open_t is not None or pending is not None:
            continue
        if not _is_signal_close(now_end, sig_min):
            continue
        window = bars[max(0, i + 1 - ANALYSIS_WINDOW_BARS):i + 1]
        a = analyse(window, now_end, strat.signal)
        if a.direction is None:
            continue
        if a.vetoes or a.spot is None:
            rejected += 1
            continue
        spot = a.spot
        atm = int(round(spot / p.strike_step) * p.strike_step)
        expiry = _expiry_for(d, p.expiry_weekday)
        strike = atm
        mid = pricer.mid(a.direction, spot, strike, now_end, expiry)
        if mid < strat.contract.min_atm_premium and strat.contract.max_itm_steps >= 1:
            strike = atm - p.strike_step if a.direction is OptType.CE else atm + p.strike_step
            mid = pricer.mid(a.direction, spot, strike, now_end, expiry)
        delta = pricer.delta(a.direction, spot, strike, now_end, expiry)
        _, ask = pricer.bid_ask(mid)
        spread = float(ask) - mid
        liq = 10 if spread * 2 <= 0.5 * strat.contract.max_spread_pct * mid else 6
        score = a.total_score(liq)
        limit = round_to_tick(ask * (1 + Decimal(str(risk_cfg.orders.entry_slippage_pct))), TICK, up=True)
        under_stop = None
        if a.invalidation is not None:
            under_stop = ask - Decimal(str(abs(spot - a.invalidation) * delta))
        plan = TradePlan.build(a.direction, limit, 1, p.lot_size, strat.exits, now_end, a.invalidation, under_stop)
        ctx = EntryContext(now=now_end, direction=a.direction, score=score, entry_price=limit, stop_price=plan.stop,
                           exit_slippage=plan.stop * Decimal(str(p.slippage_pct)), lot_size=p.lot_size,
                           available_funds=Decimal(p.starting_capital) + sum((t.net for t in trades), Decimal(0)),
                           open_positions=0, pending_entries=0, has_position_in_contract=False, data_problems=(),
                           is_trading_day=True, is_expiry_day=d.weekday() == p.expiry_weekday)
        dec = risk.check_entry(ctx)
        if not dec.approved:
            rejected += 1
            continue
        pending = dict(opt=a.direction, strike=strike, expiry=expiry, limit=limit, qty=dec.quantity,
                       invalidation=a.invalidation, under_stop=under_stop, score=score)

    results = [TradeResult(t.entry_time, t.exit_time, float(t.net), t.exit_reason) for t in trades]
    return BacktestResult(trades=trades, metrics=compute_metrics(results, p.starting_capital),
                          rejected_signals=rejected, modelled_prices=True,
                          params={"lot_size": p.lot_size, "strike_step": p.strike_step, "iv": p.iv,
                                  "slippage_pct": p.slippage_pct, "spread_pct": p.spread_pct})


# --------------------------------------------------------------------------- regimes
def day_regimes(bars: list[Bar]) -> dict[date, set[str]]:
    """Label each session: trend/range, high/low volatility, gap, using only that day's and
    the previous day's data (labels are for reporting, never for trading decisions)."""
    by_day: dict[date, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.ts.date(), []).append(b)
    days = sorted(by_day)
    ranges = {d: (max(b.high for b in by_day[d]) - min(b.low for b in by_day[d])) / by_day[d][0].open
              for d in days}
    med = sorted(ranges.values())[len(ranges) // 2] if ranges else 0
    out: dict[date, set[str]] = {}
    for k, d in enumerate(days):
        bs = by_day[d]
        tags = set()
        body = abs(bs[-1].close - bs[0].open)
        rng = max(b.high for b in bs) - min(b.low for b in bs)
        tags.add("trending" if rng and body / rng >= 0.6 else "sideways")
        tags.add("bull" if bs[-1].close > bs[0].open else "bear")
        tags.add("high_vol" if ranges[d] > med else "low_vol")
        if k > 0:
            prev_close = by_day[days[k - 1]][-1].close
            if abs(bs[0].open / prev_close - 1) > 0.005:
                tags.add("gap")
        out[d] = tags
    return out


def by_regime(result: BacktestResult, regimes: dict[date, set[str]], capital: float) -> dict[str, dict]:
    groups: dict[str, list[TradeResult]] = {}
    for t in result.trades:
        for tag in regimes.get(t.entry_time.date(), set()):
            groups.setdefault(tag, []).append(TradeResult(t.entry_time, t.exit_time, float(t.net), t.exit_reason))
    return {k: compute_metrics(v, capital) for k, v in sorted(groups.items())}


# --------------------------------------------------------------------------- walk-forward
@dataclass
class Fold:
    train: tuple[date, date]
    test: tuple[date, date]
    chosen: dict
    train_metrics: dict
    test_result: BacktestResult


def walk_forward(bars: list[Bar], base: StrategyConfig, risk_cfg: RiskConfig, rates: ChargeRates,
                 p: BacktestParams, grid: list[dict], train_days: int, test_days: int,
                 min_train_trades: int = 20) -> list[Fold]:
    """Rolling walk-forward. For each fold, pick the grid point with the best TRAIN profit
    factor (with a minimum trade count), then run it untouched on the next TEST window.
    Only test-window results should be judged."""
    days = sorted({b.ts.date() for b in bars})
    folds: list[Fold] = []
    k = 6  # leave warm-up sessions
    while k + train_days + test_days <= len(days):
        tr = (days[k], days[k + train_days - 1])
        te = (days[k + train_days], days[k + train_days + test_days - 1])
        best, best_pf, best_m = None, -1.0, {}
        for g in grid:
            cfg = _apply(base, g)
            r = run_backtest(bars, cfg, risk_cfg, rates, p, *tr)
            pf = r.metrics["profit_factor"] or 0.0
            if r.metrics["total_trades"] >= min_train_trades and pf > best_pf:
                best, best_pf, best_m = g, pf, r.metrics
        if best is None:
            best, best_m = grid[0], {}
        test = run_backtest(bars, _apply(base, best), risk_cfg, rates, p, *te)
        folds.append(Fold(tr, te, best, best_m, test))
        k += test_days
    return folds


def _apply(base: StrategyConfig, g: dict) -> StrategyConfig:
    sig = base.signal.model_copy(update={k[7:]: v for k, v in g.items() if k.startswith("signal.")})
    ex = base.exits.model_copy(update={k[6:]: v for k, v in g.items() if k.startswith("exits.")})
    return base.model_copy(update={"signal": sig, "exits": ex})
