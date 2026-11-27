"""Live tick stream via the official KiteTicker WebSocket client.

KiteTicker runs its own thread (Twisted) and auto-reconnects. We convert its callbacks
into ``Tick`` objects, feed ``DataHealth`` and the candle aggregators, and resubscribe on
reconnect. Any doubt about the stream is reported through ``DataHealth``, which the
engine checks before every entry.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from backend.core.clock import IST
from backend.logging.setup import register_secret
from backend.market_data.health import DataHealth
from backend.models.domain import Tick

log = logging.getLogger(__name__)


def tick_from_kite(t: dict[str, Any]) -> Tick | None:
    ts = t.get("exchange_timestamp") or t.get("last_trade_time")
    if ts is None:
        return None  # index ticks in LTP mode carry no timestamp: unusable for candles
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return Tick(instrument_token=int(t["instrument_token"]), last_price=float(t["last_price"]), ts=ts,
                volume_traded=t.get("volume_traded"), oi=t.get("oi"))


class KiteTickFeed:
    def __init__(self, api_key: str, access_token: str, health: DataHealth,
                 on_tick: Callable[[Tick], None], on_order_update: Callable[[dict], None] | None = None) -> None:
        from kiteconnect import KiteTicker

        register_secret(access_token)
        self.health = health
        self._on_tick = on_tick
        self._tokens: set[int] = set()
        self._lock = threading.Lock()
        self._ws = KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=50,
                              reconnect_max_delay=60)
        self._ws.on_ticks = self._ticks
        self._ws.on_connect = self._connect
        self._ws.on_close = self._close
        self._ws.on_error = self._error
        self._ws.on_reconnect = self._reconnect
        self._ws.on_noreconnect = self._noreconnect
        if on_order_update:
            self._ws.on_order_update = lambda ws, data: on_order_update(data)

    def subscribe(self, tokens: list[int]) -> None:
        with self._lock:
            self._tokens.update(tokens)
        if self._ws.is_connected():
            self._ws.subscribe(tokens)
            self._ws.set_mode(self._ws.MODE_FULL, tokens)

    def start(self) -> None:
        self._ws.connect(threaded=True)

    def stop(self) -> None:
        self._ws.close()

    # --- callbacks (run on the ticker thread) ---
    def _ticks(self, ws: Any, ticks: list[dict]) -> None:
        for raw in ticks:
            t = tick_from_kite(raw)
            if t is None:
                continue
            if self.health.record(t.instrument_token, t.ts, t.last_price):
                try:
                    self._on_tick(t)
                except Exception:
                    log.exception("tick handler failed")

    def _connect(self, ws: Any, response: Any) -> None:
        self.health.on_connect()
        with self._lock:
            tokens = list(self._tokens)
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
        log.info("market data connected", extra={"tokens": len(tokens)})

    def _close(self, ws: Any, code: Any, reason: Any) -> None:
        self.health.on_disconnect()
        log.warning("market data closed", extra={"code": code, "reason": str(reason)})

    def _error(self, ws: Any, code: Any, reason: Any) -> None:
        self.health.on_disconnect()
        log.error("market data error", extra={"code": code, "reason": str(reason)})

    def _reconnect(self, ws: Any, attempts: int) -> None:
        self.health.on_reconnect()
        log.warning("market data reconnecting", extra={"attempt": attempts})

    def _noreconnect(self, ws: Any) -> None:
        self.health.on_disconnect()
        log.critical("market data gave up reconnecting")
