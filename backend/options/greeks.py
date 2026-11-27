"""Black-Scholes pricing, delta and implied volatility for European index options.

NSE index options are European-style and cash-settled. Used for delta filtering in
contract selection and for modelled option prices in backtests (clearly labelled as
approximations). Rates are continuously compounded; dividends are ignored.
"""

from __future__ import annotations

import math

from backend.models.domain import OptType


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(s: float, k: float, t: float, r: float, vol: float) -> tuple[float, float]:
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return d1, d1 - vol * math.sqrt(t)


def bs_price(opt: OptType, s: float, k: float, t: float, r: float, vol: float) -> float:
    if t <= 0 or vol <= 0:
        return max(0.0, s - k) if opt is OptType.CE else max(0.0, k - s)
    d1, d2 = _d1_d2(s, k, t, r, vol)
    if opt is OptType.CE:
        return s * _cdf(d1) - k * math.exp(-r * t) * _cdf(d2)
    return k * math.exp(-r * t) * _cdf(-d2) - s * _cdf(-d1)


def bs_delta(opt: OptType, s: float, k: float, t: float, r: float, vol: float) -> float:
    if t <= 0 or vol <= 0:
        itm = s > k if opt is OptType.CE else s < k
        return (1.0 if itm else 0.0) * (1 if opt is OptType.CE else -1)
    d1, _ = _d1_d2(s, k, t, r, vol)
    return _cdf(d1) if opt is OptType.CE else _cdf(d1) - 1


def bs_vega(s: float, k: float, t: float, r: float, vol: float) -> float:
    d1, _ = _d1_d2(s, k, t, r, vol)
    return s * _pdf(d1) * math.sqrt(t)


def implied_vol(opt: OptType, price: float, s: float, k: float, t: float, r: float,
                lo: float = 0.01, hi: float = 3.0, tol: float = 1e-6) -> float | None:
    """Bisection IV. Returns None when the price is outside no-arbitrage bounds."""
    if t <= 0 or price <= 0:
        return None
    if price < bs_price(opt, s, k, t, r, lo) - tol or price > bs_price(opt, s, k, t, r, hi) + tol:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if bs_price(opt, s, k, t, r, mid) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2
