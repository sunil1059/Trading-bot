import copy

import pytest
import yaml
from pydantic import ValidationError

from backend.core.config import DEFAULT_RISK_FILE, AppSettings, RiskConfig, TradingMode


@pytest.fixture
def raw():
    return yaml.safe_load(DEFAULT_RISK_FILE.read_text())


def test_shipped_risk_config_is_valid():
    cfg = RiskConfig.from_yaml()
    assert cfg.capital.total_capital == 100000
    assert cfg.trades.max_open_positions == 1
    assert cfg.session.allow_overnight is False


def test_default_mode_is_paper_and_live_disabled(monkeypatch):
    for k in ("TB_MODE", "TB_LIVE_TRADING_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    s = AppSettings(_env_file=None)
    assert s.mode is TradingMode.PAPER
    assert s.live_trading_enabled is False
    assert s.live_mode_allowed() is False


def test_live_mode_alone_does_not_allow_live():
    s = AppSettings(_env_file=None, mode="live")
    assert s.live_mode_allowed() is False


@pytest.mark.parametrize(
    "path,value",
    [
        (("capital", "max_loss_per_trade"), 3001),  # > 3% of 1L
        (("capital", "max_daily_loss"), 6001),  # > 6%
        (("capital", "max_capital_per_trade"), 25001),  # > 25%
        (("trades", "max_trades_per_day"), 6),
        (("trades", "max_open_positions"), 3),
        (("trades", "stop_after_consecutive_losses"), 4),
        (("session", "first_entry"), "09:15"),
        (("session", "square_off"), "15:20"),
        (("session", "allow_overnight"), True),
        (("orders", "entry_slippage_pct"), 0.05),
        (("min_signal_score_for_entry",), 40),
    ],
)
def test_hard_ceilings_reject_loose_values(raw, path, value):
    data = copy.deepcopy(raw)
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        RiskConfig.model_validate(data)


def test_unknown_keys_rejected(raw):
    raw["capital"]["leverage"] = 5
    with pytest.raises(ValidationError):
        RiskConfig.model_validate(raw)


def test_secrets_not_in_repr():
    s = AppSettings(_env_file=None, kite_api_secret="supersecretvalue123")
    assert "supersecretvalue123" not in repr(s)
    assert "supersecretvalue123" not in str(s.model_dump())
