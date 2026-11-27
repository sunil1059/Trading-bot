"""Strategy parameters (config/strategy.yaml). Starting points for paper testing, not tuned values."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import PROJECT_ROOT

DEFAULT_STRATEGY_FILE = PROJECT_ROOT / "config" / "strategy.yaml"


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SignalParams(_S):
    underlying: str = "NIFTY"
    underlying_exchange: str = "NSE"
    options_exchange: str = "NFO"
    opening_range_end: time = time(9, 30)
    signal_minutes: int = 5
    trend_minutes: int = 15
    min_relative_volume: float = Field(1.5, gt=0)
    rvol_lookback: int = 20
    rsi_ce_band: tuple[float, float] = (55, 75)
    rsi_pe_band: tuple[float, float] = (25, 45)
    breakout_max_age_bars: int = Field(2, ge=1, description="Breakout must have happened within this many signal bars")
    min_room_to_stop_ratio: float = Field(1.5, gt=0)
    invalidation_atr_buffer: float = Field(0.1, ge=0)
    max_vix: float | None = 25.0
    max_gap_pct: float = Field(1.0, gt=0, description="Skip day if |open/prev close - 1| exceeds this %")


class ContractParams(_S):
    max_itm_steps: int = Field(1, ge=0, le=2)
    min_atm_premium: float = Field(40.0, ge=0, description="Below this ATM premium, move one strike ITM")
    max_spread_pct: float = Field(0.01, gt=0)
    max_spread_abs: float = Field(1.0, gt=0)
    max_quote_age_seconds: int = Field(5, ge=1)
    max_last_trade_age_seconds: int = Field(10, ge=1)
    min_volume: int = Field(100_000, ge=0)
    min_oi: int = Field(50_000, ge=0)
    delta_band: tuple[float, float] = (0.45, 0.65)
    risk_free_rate: float = 0.065
    max_ltp_outside_book_pct: float = Field(0.05, gt=0)


class ExitParams(_S):
    stop_premium_pct: float = Field(0.25, gt=0, lt=1)
    target_r: float = Field(2.0, gt=0)
    breakeven_at_r: float = Field(1.0, gt=0)
    partial_at_r: float | None = 1.5
    trail_after_partial_pct: float = Field(0.15, gt=0, lt=1)
    time_stop_minutes: int = Field(30, ge=1)
    time_stop_min_r: float = 0.5
    exit_on_vwap_cross: bool = True


class StrategyConfig(_S):
    signal: SignalParams = SignalParams()
    contract: ContractParams = ContractParams()
    exits: ExitParams = ExitParams()

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_STRATEGY_FILE) -> StrategyConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
