"""Application and risk configuration.

Two sources:
- Environment variables (prefix ``TB_``), loaded from ``.env`` for secrets and deployment settings.
- ``config/risk.yaml`` for trading and risk limits.

Every risk limit is validated against HARD CEILINGS defined in code. A config file
cannot loosen these; it can only be stricter. LIVE mode is never enabled by config
alone (see ``live_mode_allowed``).
"""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RISK_FILE = PROJECT_ROOT / "config" / "risk.yaml"


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    SIGNAL_ONLY = "signal_only"
    LIVE = "live"


class EmergencyExitPolicy(StrEnum):
    EXIT_ALL = "exit_all"  # square off every open position immediately
    KEEP_STOPS = "keep_stops"  # stop new entries, leave existing stops/targets running


# ---------------------------------------------------------------------------
# Hard ceilings. Changing these requires a code change and review, by design.
# ---------------------------------------------------------------------------
class HardCeilings:
    MAX_CAPITAL_PER_TRADE_PCT = 0.25
    MAX_LOSS_PER_TRADE_PCT = 0.03
    MAX_DAILY_LOSS_PCT = 0.06
    MAX_TRADES_PER_DAY = 5
    MAX_OPEN_POSITIONS = 2
    MAX_CONSECUTIVE_LOSSES_STOP = 3
    EARLIEST_ENTRY = time(9, 20)
    LATEST_ENTRY = time(14, 45)
    LATEST_SQUARE_OFF = time(15, 15)
    MAX_ENTRY_SLIPPAGE_PCT = 0.02


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapitalLimits(_Strict):
    total_capital: float = Field(gt=0, description="Total trading capital in INR.")
    max_capital_per_trade: float = Field(gt=0, description="Max premium x quantity per trade, INR.")
    max_loss_per_trade: float = Field(gt=0, description="Max loss per trade incl. charges and slippage, INR.")
    max_daily_loss: float = Field(gt=0, description="Realised + MTM loss that stops trading for the day, INR.")
    max_weekly_loss: float = Field(gt=0, description="Loss that pauses trading until manual reset, INR.")

    @model_validator(mode="after")
    def _check_ceilings(self) -> Self:
        c = self.total_capital
        if self.max_capital_per_trade > c * HardCeilings.MAX_CAPITAL_PER_TRADE_PCT:
            raise ValueError(
                f"max_capital_per_trade exceeds hard ceiling of "
                f"{HardCeilings.MAX_CAPITAL_PER_TRADE_PCT:.0%} of total_capital"
            )
        if self.max_loss_per_trade > c * HardCeilings.MAX_LOSS_PER_TRADE_PCT:
            raise ValueError(
                f"max_loss_per_trade exceeds hard ceiling of {HardCeilings.MAX_LOSS_PER_TRADE_PCT:.0%} of total_capital"
            )
        if self.max_daily_loss > c * HardCeilings.MAX_DAILY_LOSS_PCT:
            raise ValueError(
                f"max_daily_loss exceeds hard ceiling of {HardCeilings.MAX_DAILY_LOSS_PCT:.0%} of total_capital"
            )
        if self.max_loss_per_trade > self.max_daily_loss:
            raise ValueError("max_loss_per_trade cannot exceed max_daily_loss")
        if self.max_daily_loss > self.max_weekly_loss:
            raise ValueError("max_daily_loss cannot exceed max_weekly_loss")
        return self


class TradeLimits(_Strict):
    max_trades_per_day: int = Field(ge=1, le=HardCeilings.MAX_TRADES_PER_DAY)
    max_open_positions: int = Field(ge=1, le=HardCeilings.MAX_OPEN_POSITIONS)
    pause_after_consecutive_losses: int = Field(ge=1)
    pause_minutes: int = Field(ge=0)
    stop_after_consecutive_losses: int = Field(ge=1, le=HardCeilings.MAX_CONSECUTIVE_LOSSES_STOP)
    same_direction_reentry_cooldown_minutes: int = Field(ge=0)

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.pause_after_consecutive_losses > self.stop_after_consecutive_losses:
            raise ValueError("pause_after_consecutive_losses must be <= stop_after_consecutive_losses")
        return self


class SessionWindow(_Strict):
    first_entry: time
    last_entry: time
    square_off: time
    allow_overnight: bool = False
    allow_expiry_day_entries: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.first_entry < HardCeilings.EARLIEST_ENTRY:
            raise ValueError(f"first_entry earlier than hard limit {HardCeilings.EARLIEST_ENTRY}")
        if self.last_entry > HardCeilings.LATEST_ENTRY:
            raise ValueError(f"last_entry later than hard limit {HardCeilings.LATEST_ENTRY}")
        if self.square_off > HardCeilings.LATEST_SQUARE_OFF:
            raise ValueError(f"square_off later than hard limit {HardCeilings.LATEST_SQUARE_OFF}")
        if not (self.first_entry < self.last_entry < self.square_off):
            raise ValueError("require first_entry < last_entry < square_off")
        if self.allow_overnight:
            raise ValueError("overnight positions are not supported in v1")
        return self


class OrderPolicy(_Strict):
    entry_slippage_pct: float = Field(gt=0, le=HardCeilings.MAX_ENTRY_SLIPPAGE_PCT)
    entry_timeout_seconds: int = Field(ge=1, le=60)
    exit_slippage_pct: float = Field(gt=0, le=0.05)
    exit_timeout_seconds: int = Field(ge=1, le=60)


class RiskConfig(_Strict):
    capital: CapitalLimits
    trades: TradeLimits
    session: SessionWindow
    orders: OrderPolicy
    emergency_exit_policy: EmergencyExitPolicy = EmergencyExitPolicy.EXIT_ALL
    min_signal_score_for_entry: int = Field(ge=50, le=100)

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_RISK_FILE) -> RiskConfig:
        with path.open() as fh:
            data = yaml.safe_load(fh)
        return cls.model_validate(data)


class AppSettings(BaseSettings):
    """Deployment settings and secrets from environment / .env. Never logged in full."""

    model_config = SettingsConfigDict(env_prefix="TB_", env_file=".env", extra="ignore")

    mode: TradingMode = TradingMode.PAPER
    # LIVE requires this AND the runtime unlock checks; config alone never enables it.
    live_trading_enabled: bool = False

    database_url: str = "sqlite:///./data/trading_bot.db"
    log_level: str = "INFO"
    log_json: bool = True
    risk_config_path: Path = DEFAULT_RISK_FILE

    kite_api_key: SecretStr | None = None
    kite_api_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    dashboard_secret_key: SecretStr | None = None

    def live_mode_allowed(self) -> bool:
        """Static precondition only. Runtime checks (auth, health, calendar, user
        confirmation, no emergency lock) are enforced separately before LIVE starts."""
        return self.mode is TradingMode.LIVE and self.live_trading_enabled

    def load_risk(self) -> RiskConfig:
        return RiskConfig.from_yaml(self.risk_config_path)
