"""Transaction cost model for option buying (Zerodha rates from config/charges.yaml)."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from backend.core.config import PROJECT_ROOT

DEFAULT_CHARGES_FILE = PROJECT_ROOT / "config" / "charges.yaml"
CRORE = Decimal(10_000_000)
PAISE = Decimal("0.01")


class ChargeRates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    as_of: date
    brokerage_per_order: Decimal
    stt_sell_pct: Decimal
    exchange_txn_pct: Decimal
    sebi_per_crore: Decimal
    stamp_buy_pct: Decimal
    ipft_per_crore: Decimal
    gst_pct: Decimal

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_CHARGES_FILE) -> ChargeRates:
        return cls.model_validate(yaml.safe_load(path.read_text()))


def leg_charges(rates: ChargeRates, premium: Decimal, quantity: int, is_buy: bool, orders: int = 1) -> Decimal:
    """All charges for one side of a trade (one or more executed orders)."""
    turnover = premium * quantity
    brokerage = rates.brokerage_per_order * orders if quantity > 0 else Decimal(0)
    stt = Decimal(0) if is_buy else turnover * rates.stt_sell_pct / 100
    txn = turnover * rates.exchange_txn_pct / 100
    sebi = turnover * rates.sebi_per_crore / CRORE
    ipft = turnover * rates.ipft_per_crore / CRORE
    stamp = turnover * rates.stamp_buy_pct / 100 if is_buy else Decimal(0)
    gst = (brokerage + txn + sebi + ipft) * rates.gst_pct / 100
    return (brokerage + stt + txn + sebi + ipft + stamp + gst).quantize(PAISE, rounding=ROUND_HALF_UP)


def round_trip_charges(rates: ChargeRates, entry: Decimal, exit_: Decimal, quantity: int) -> Decimal:
    return leg_charges(rates, entry, quantity, True) + leg_charges(rates, exit_, quantity, False)
