from datetime import date, datetime, timezone
import pytest
from app.services.collection_calendar import automatic_collection_allowed, manual_failure_isolated

@pytest.mark.parametrize('day,allowed', [('2026-11-06',True),('2026-11-07',False),
    ('2026-11-08',False),('2026-10-01',False),('2026-10-10',False),
    ('2026-10-08',True),('2027-01-04',False)])
def test_automatic_calendar_fails_closed(day,allowed):
    assert automatic_collection_allowed(date.fromisoformat(day)) is allowed
    assert manual_failure_isolated(date.fromisoformat(day)) is not allowed

def test_china_midnight():
    assert automatic_collection_allowed(datetime(2026,11,6,15,59,tzinfo=timezone.utc))
    assert not automatic_collection_allowed(datetime(2026,11,6,16,0,tzinfo=timezone.utc))
