"""NSE trading calendar.

There is no official machine-readable NSE holiday API, so holidays are loaded from
``config/nse_calendar.yaml``, which must be filled from the official NSE holiday
circular for each year. If the current year is missing, the calendar raises
``CalendarUnavailable`` and the bot must not open trades: we never guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from backend.core.clock import IST, to_ist
from backend.core.config import PROJECT_ROOT

DEFAULT_CALENDAR_FILE = PROJECT_ROOT / "config" / "nse_calendar.yaml"


class CalendarUnavailable(RuntimeError):
    """Raised when the calendar has no verified data for the requested year."""


@dataclass(frozen=True)
class MarketHours:
    open: time = time(9, 15)
    close: time = time(15, 30)


@dataclass
class TradingCalendar:
    hours: MarketHours
    holidays: dict[int, set[date]] = field(default_factory=dict)
    # Extra sessions on weekends/holidays (e.g. special or Muhurat sessions) need explicit opt-in.
    special_sessions: dict[date, tuple[time, time]] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_CALENDAR_FILE) -> TradingCalendar:
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
        hours_raw = data.get("market_hours", {})
        hours = MarketHours(
            open=_parse_time(hours_raw.get("open", "09:15")),
            close=_parse_time(hours_raw.get("close", "15:30")),
        )
        holidays: dict[int, set[date]] = {}
        for year, entries in (data.get("holidays") or {}).items():
            if entries is None:
                continue  # year listed but not yet filled: treat as unavailable
            days = {_parse_date(e["date"]) for e in entries}
            if any(d.year != int(year) for d in days):
                raise ValueError(f"holiday listed under {year} has a different year")
            holidays[int(year)] = days
        specials = {
            _parse_date(s["date"]): (_parse_time(s["open"]), _parse_time(s["close"]))
            for s in (data.get("special_sessions") or [])
        }
        return cls(hours=hours, holidays=holidays, special_sessions=specials)

    def _require_year(self, year: int) -> set[date]:
        if year not in self.holidays:
            raise CalendarUnavailable(
                f"No verified NSE holiday list for {year}. Add it to config/nse_calendar.yaml "
                "from the official NSE circular before trading."
            )
        return self.holidays[year]

    def is_trading_day(self, d: date) -> bool:
        holidays = self._require_year(d.year)
        if d in self.special_sessions:
            return False  # special sessions are not regular trading days; opt-in only
        return d.weekday() < 5 and d not in holidays

    def is_market_open(self, at: datetime) -> bool:
        at = to_ist(at)
        if not self.is_trading_day(at.date()):
            return False
        return self.hours.open <= at.time() < self.hours.close

    def next_trading_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        for _ in range(15):
            if self.is_trading_day(nxt):
                return nxt
            nxt += timedelta(days=1)
        raise CalendarUnavailable("no trading day found within 15 days")


def _parse_time(v: str | time) -> time:
    return v if isinstance(v, time) else time.fromisoformat(str(v))


def _parse_date(v: str | date) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


__all__ = ["IST", "CalendarUnavailable", "MarketHours", "TradingCalendar"]
