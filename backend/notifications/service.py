"""Notifications: dashboard alerts (always), Telegram and email (optional).

Every message is passed through log redaction before it leaves the process, and a
failing channel never affects trading.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol

import httpx

from backend.core.clock import Clock
from backend.logging.setup import redact_text, register_secret

log = logging.getLogger(__name__)

EVENTS = {
    "SIGNAL", "TRADE_REJECTED", "ORDER_PLACED", "ORDER_EXECUTED", "STOP_LOSS", "TARGET", "TRADE_EXITED",
    "DAILY_LOSS_LIMIT", "BOT_PAUSED", "BROKER_DISCONNECTED", "EMERGENCY_STOP", "RISK", "INFO",
}


@dataclass(frozen=True)
class Alert:
    ts: datetime
    event: str
    severity: str  # info | warning | critical
    message: str


class Channel(Protocol):
    def send(self, alert: Alert) -> None: ...


class TelegramChannel:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 5.0) -> None:
        register_secret(bot_token)  # masks it even inside exception text
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id, self._timeout = chat_id, timeout

    def send(self, alert: Alert) -> None:
        text = f"[{alert.severity.upper()}] {alert.event}\n{alert.message}"
        r = httpx.post(self._url, json={"chat_id": self._chat_id, "text": text}, timeout=self._timeout)
        if r.status_code != 200:
            raise RuntimeError(f"telegram HTTP {r.status_code}")


class EmailChannel:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str, to: str) -> None:
        self.host, self.port, self.user, self._password, self.sender, self.to = host, port, user, password, sender, to

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[trading-bot] {alert.severity.upper()} {alert.event}"
        msg["From"], msg["To"] = self.sender, self.to
        msg.set_content(alert.message)
        with smtplib.SMTP_SSL(self.host, self.port, context=ssl.create_default_context(), timeout=10) as s:
            s.login(self.user, self._password)
            s.send_message(msg)


class Notifier:
    def __init__(self, clock: Clock, channels: list[Channel] | None = None, min_external_severity: str = "info",
                 keep: int = 500) -> None:
        self.clock = clock
        self.channels = channels or []
        self.alerts: deque[Alert] = deque(maxlen=keep)
        self._min = ["info", "warning", "critical"].index(min_external_severity)
        self._lock = threading.Lock()

    def notify(self, event: str, message: str, severity: str = "info") -> Alert:
        if event not in EVENTS:
            event = "INFO"
        alert = Alert(self.clock.now(), event, severity, redact_text(message))
        with self._lock:
            self.alerts.appendleft(alert)
        if ["info", "warning", "critical"].index(severity) >= self._min:
            for ch in self.channels:
                threading.Thread(target=self._safe_send, args=(ch, alert), daemon=True).start()
        return alert

    @staticmethod
    def _safe_send(ch: Channel, alert: Alert) -> None:
        try:
            ch.send(alert)
        except Exception as e:  # noqa: BLE001 - a channel failure must never affect trading
            log.warning("notification channel failed", extra={"channel": type(ch).__name__, "err": str(e)})
