"""Instrument discovery from the broker's instrument master. Nothing is hardcoded."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backend.models.domain import Instrument, OptType


class InstrumentError(RuntimeError):
    pass


@dataclass
class OptionUniverse:
    """All option contracts of one underlying, built from the day's instrument master."""

    underlying: str
    contracts: list[Instrument]

    @classmethod
    def build(cls, instruments: list[Instrument], underlying: str) -> OptionUniverse:
        contracts = [i for i in instruments if i.name == underlying and i.is_option and i.expiry and i.strike]
        if not contracts:
            raise InstrumentError(f"no option contracts for {underlying} in instrument master")
        lots = {i.lot_size for i in contracts}
        if any(lot <= 0 for lot in lots):
            raise InstrumentError("invalid lot size in instrument master")
        return cls(underlying=underlying, contracts=contracts)

    def expiries(self, on_or_after: date | None = None) -> list[date]:
        exps = sorted({c.expiry for c in self.contracts if c.expiry})
        return [e for e in exps if on_or_after is None or e >= on_or_after]

    def strikes(self, expiry: date) -> list[Decimal]:
        return sorted({c.strike for c in self.contracts if c.expiry == expiry and c.strike is not None})

    def strike_step(self, expiry: date) -> Decimal:
        s = self.strikes(expiry)
        if len(s) < 2:
            raise InstrumentError("not enough strikes to infer strike step")
        return min(b - a for a, b in zip(s, s[1:], strict=False))

    def lot_size(self, expiry: date) -> int:
        lots = {c.lot_size for c in self.contracts if c.expiry == expiry}
        if len(lots) != 1:
            raise InstrumentError(f"ambiguous lot size for {expiry}: {sorted(lots)}")
        return lots.pop()

    def contract(self, expiry: date, strike: Decimal, opt: OptType) -> Instrument:
        for c in self.contracts:
            if c.expiry == expiry and c.strike == strike and c.instrument_type == opt.value:
                return c
        raise InstrumentError(f"no {opt} contract {strike} {expiry}")

    def atm_strike(self, expiry: date, spot: float) -> Decimal:
        s = self.strikes(expiry)
        if not s:
            raise InstrumentError("no strikes")
        return min(s, key=lambda k: (abs(float(k) - spot), k))
