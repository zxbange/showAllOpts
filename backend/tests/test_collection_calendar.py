from datetime import date, datetime, timezone
import pytest
from app.services.collection_calendar import CHINA, automatic_collection_allowed, latest_due_slot, manual_failure_isolated

@pytest.mark.parametrize('day,allowed', [('2026-11-06',True),('2026-11-07',False),
    ('2026-11-08',False),('2026-10-01',False),('2026-10-10',False),
    ('2026-10-08',True),('2027-01-04',False)])
def test_automatic_calendar_fails_closed(day,allowed):
    assert automatic_collection_allowed(date.fromisoformat(day)) is allowed
    assert manual_failure_isolated(date.fromisoformat(day)) is not allowed

def test_china_midnight():
    assert automatic_collection_allowed(datetime(2026,11,6,15,59,tzinfo=timezone.utc))
    assert not automatic_collection_allowed(datetime(2026,11,6,16,0,tzinfo=timezone.utc))


@pytest.mark.parametrize(('requested_at', 'expected'), [
    ('2026-11-25T07:59:59+08:00', '2026-11-24T18:00:00+08:00'),
    ('2026-11-25T00:00:00+00:00', '2026-11-25T08:00:00+08:00'),
    ('2026-11-25T10:00:00+00:00', '2026-11-25T18:00:00+08:00'),
])
def test_most_recent_configured_cutoff_uses_beijing_time_and_includes_boundary(requested_at, expected):
    assert latest_due_slot(datetime.fromisoformat(requested_at), ['18:00', '08:00']).isoformat() == expected


def test_due_slot_skips_only_known_closed_days_never_guesses_unknown_coverage():
    requested = datetime(2026, 10, 8, 7, 59, tzinfo=CHINA)
    valid = lambda day: False if day.month == 10 else True
    assert latest_due_slot(requested, ['08:00'], is_open=valid).isoformat() == '2026-09-30T08:00:00+08:00'
    assert latest_due_slot(requested, ['08:00'], is_open=lambda _: None) is None
    assert latest_due_slot(requested, ['08:00'], is_open=lambda _: False) is None


@pytest.mark.parametrize('times', [None, [], '08:00', ['8:00'], ['24:00'], ['08:60'], [8], ['08:00', 'bad']])
def test_due_slot_rejects_missing_or_malformed_times(times):
    with pytest.raises(ValueError, match='HH:MM'):
        latest_due_slot(datetime(2026, 11, 24, 16, tzinfo=CHINA), times)


def test_due_slot_requires_an_aware_request_timestamp():
    with pytest.raises(ValueError, match='Timezone-aware'):
        latest_due_slot(datetime(2026, 11, 24, 16), ['08:00'])
