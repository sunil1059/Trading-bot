"""Process runtime: builds the engine for the configured mode and runs it on a thread.

Credentials never touch the database or logs. The Kite access token lives in memory
only; after a restart (or the next morning) you log in again from the dashboard.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from backend.brokers.base import AuthError, BrokerBase
from backend.core.calendar import TradingCalendar
from backend.core.clock import Clock, SystemClock
from backend.core.config import PROJECT_ROOT, AppSettings, TradingMode
from backend.database.session import make_engine, make_session_factory
from backend.market_data.health import DataHealth
from backend.notifications.service import Channel, EmailChannel, Notifier, TelegramChannel
from backend.risk.charges import ChargeRates
from backend.services.engine import TradingEngine
from backend.strategies.config import StrategyConfig

log = logging.getLogger(__name__)
EVENTS_FILE = PROJECT_ROOT / "config" / "events.yaml"


class LiveModeUnavailable(RuntimeError):
    pass


def load_event_days(path: Path = EVENTS_FILE) -> set[date]:
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text()) or {}
    return {date.fromisoformat(str(e["date"])) for e in (data.get("events") or [])}


def build_channels(settings: AppSettings) -> list[Channel]:
    chans: list[Channel] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        chans.append(TelegramChannel(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id))
    if settings.smtp_host and settings.smtp_password and settings.alert_email_to:
        chans.append(EmailChannel(settings.smtp_host, settings.smtp_port, settings.smtp_user or "",
                                  settings.smtp_password.get_secret_value(), settings.smtp_user or "",
                                  settings.alert_email_to))
    return chans


class Runtime:
    def __init__(self, settings: AppSettings, clock: Clock | None = None) -> None:
        if settings.mode is TradingMode.LIVE:
            # Phase 10 gate: LIVE execution is deliberately not wired in this build.
            raise LiveModeUnavailable("LIVE mode is locked in this build. Run PAPER or SIGNAL_ONLY.")
        if settings.mode is TradingMode.BACKTEST:
            raise LiveModeUnavailable("use scripts/run_backtest.py for backtests")
        self.settings = settings
        self.clock = clock or SystemClock()
        self.risk_cfg = settings.load_risk()
        self.strat = StrategyConfig.from_yaml()
        self.rates = ChargeRates.from_yaml()
        self.calendar = TradingCalendar.from_yaml()
        self.db = make_engine(settings.database_url)
        self.sessions = make_session_factory(self.db)
        self.notifier = Notifier(self.clock, build_channels(settings))
        self.health = DataHealth(clock=self.clock)
        self.engine: TradingEngine | None = None
        self.feed = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def state(self) -> str:
        if self.engine is None:
            return "awaiting_login" if self.last_error is None else "error"
        return "running" if self._thread and self._thread.is_alive() else "stopped"

    def start_with_token(self, access_token: str) -> None:
        from backend.brokers.paper import PaperBroker
        from backend.brokers.zerodha import ZerodhaBroker
        from backend.market_data.feed import KiteTickFeed

        api_key = self.settings.kite_api_key
        if api_key is None:
            raise AuthError("TB_KITE_API_KEY not configured")
        self.stop()
        data = ZerodhaBroker.from_credentials(api_key.get_secret_value(), access_token)
        broker: BrokerBase = PaperBroker(data, self.clock, Decimal(self.risk_cfg.capital.total_capital), self.rates)
        eng = TradingEngine(mode=self.settings.mode, risk_cfg=self.risk_cfg, strat=self.strat, rates=self.rates,
                            data=data, broker=broker, sessions=self.sessions, clock=self.clock,
                            calendar=self.calendar, notifier=self.notifier, health=self.health,
                            event_days=load_event_days())
        eng.start()
        feed = KiteTickFeed(api_key.get_secret_value(), access_token, self.health, eng.on_tick)
        feed.subscribe(eng.tokens_to_stream())
        feed.start()
        self.engine, self.feed, self.last_error = eng, feed, None
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self.engine is not None
        while not self._stop.wait(2.0):
            try:
                self.engine.step()
                if self.feed is not None:
                    self.feed.subscribe(self.engine.tokens_to_stream())
            except AuthError as e:
                self.engine.auth_ok = False
                self.last_error = f"broker session expired: {e}"
                self.notifier.notify("BROKER_DISCONNECTED", "Kite session expired: log in again", "critical")
                self._stop.wait(30)
            except Exception as e:  # noqa: BLE001 - keep the loop alive; entries stay blocked by health checks
                log.exception("engine step failed")
                self.last_error = str(e)
                self.notifier.notify("RISK", f"Engine error: {e}", "critical")
                self._stop.wait(5)

    def stop(self) -> None:
        self._stop.set()
        if self.feed is not None:
            try:
                self.feed.stop()
            except Exception:  # noqa: BLE001
                log.warning("feed stop failed")
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None
