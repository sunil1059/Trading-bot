from datetime import date, datetime, time, timedelta

import pytest

from backend.core.calendar import CalendarUnavailable, MarketHours, TradingCalendar
from backend.core.clock import IST, FixedClock, ist


@pytest.fixture
def cal():
    # Test-only holiday; not a real NSE date claim.
    return TradingCalendar(hours=MarketHours(), holidays={2030: {date(2030, 1, 2)}})


def test_unknown_year_refuses(cal):
    with pytest.raises(CalendarUnavailable):
        cal.is_trading_day(date(2031, 1, 6))


def test_shipped_calendar_has_no_guessed_years():
    cal = TradingCalendar.from_yaml()
    with pytest.raises(CalendarUnavailable):
        cal.is_trading_day(date(2026, 12, 1))


def test_weekend_and_holiday(cal):
    assert cal.is_trading_day(date(2030, 1, 1))  # Tuesday
    assert not cal.is_trading_day(date(2030, 1, 2))  # test holiday
    assert not cal.is_trading_day(date(2030, 1, 5))  # Saturday
    assert cal.next_trading_day(date(2030, 1, 1)) == date(2030, 1, 3)


def test_market_hours_boundaries(cal):
    d = date(2030, 1, 1)
    assert not cal.is_market_open(ist(d, time(9, 14, 59)))
    assert cal.is_market_open(ist(d, time(9, 15)))
    assert not cal.is_market_open(ist(d, time(15, 30)))


def test_naive_datetime_rejected(cal):
    with pytest.raises(ValueError):
        cal.is_market_open(datetime(2030, 1, 1, 10, 0))


def test_fixed_clock_never_goes_backwards():
    c = FixedClock(datetime(2030, 1, 1, 9, 15, tzinfo=IST))
    c.advance(timedelta(minutes=1))
    with pytest.raises(ValueError):
        c.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        c.set(datetime(2030, 1, 1, 9, 0, tzinfo=IST))
