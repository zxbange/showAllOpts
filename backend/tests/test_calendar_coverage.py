from datetime import date

import pytest

from app.services import holdings_calendar
from app.services.holdings_calendar import first_uncovered_day


def test_coverage_warning_precedes_unknown_year(monkeypatch):
    monkeypatch.setattr(holdings_calendar, 'CALENDARS', {2026: holdings_calendar.CALENDARS[2026]})
    assert first_uncovered_day(date(2026, 12, 1), horizon_days=45) == date(2027, 1, 1)


def test_no_warning_for_fully_covered_horizon():
    assert first_uncovered_day(date(2026, 6, 1), horizon_days=45) is None


def test_empty_coverage_horizon_rejected():
    with pytest.raises(ValueError, match='positive'):
        first_uncovered_day(date(2026, 6, 1), horizon_days=0)
