"""Validate settings and risk config, print a safe summary (no secrets). Exit 1 on error."""

from __future__ import annotations

import sys

from pydantic import ValidationError

from backend.core.config import AppSettings
from backend.logging.setup import configure_logging


def main() -> int:
    settings = AppSettings()
    configure_logging(settings.log_level, settings.log_json)
    try:
        risk = settings.load_risk()
    except (ValidationError, OSError) as exc:
        print(f"RISK CONFIG INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"mode={settings.mode.value} live_allowed={settings.live_mode_allowed()}")
    print(f"capital={risk.capital.total_capital:,.0f} max_loss_per_trade={risk.capital.max_loss_per_trade:,.0f} "
          f"max_daily_loss={risk.capital.max_daily_loss:,.0f} max_trades={risk.trades.max_trades_per_day} "
          f"max_open={risk.trades.max_open_positions}")
    print(f"kite_credentials_present={settings.kite_api_key is not None and settings.kite_api_secret is not None}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
