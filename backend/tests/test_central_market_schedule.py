import pytest
from datetime import datetime, timedelta
from app.services.market_collection import CHINA
from test_market_collection import worker as original_worker, DAY, EVENING

def worker(*args, **kwargs):
    owner, provider, writer, clock = original_worker(*args, **kwargs)
    provider.historical_capability = lambda: {"verified": True}
    return owner, provider, writer, clock


@pytest.fixture(autouse=True)
def enabled_market(monkeypatch):
    monkeypatch.setenv('MARKET_COLLECTION_WORKER_ENABLED', 'true')


def test_midnight_batch_keeps_previous_day_even_if_recovered_evening(tmp_path):
    owner, _, _, _ = worker(tmp_path)
    now = datetime(2026, 11, 11, 0, 1, tzinfo=CHINA)
    assert owner._data_cutoff(now, False) == DAY
    assert owner._data_cutoff(now.replace(hour=20), False) == DAY
    assert owner._data_cutoff(now.replace(hour=20), True).isoformat() == '2026-11-11'
    assert owner._scheduled_scope(now) == 'scheduled:2026-11-11'
    assert owner._scheduled_scope(now - timedelta(seconds=1)) == 'scheduled:2026-11-10'


def test_status_has_daily_next_slot_without_calendar(tmp_path):
    owner, _, _, _ = worker(tmp_path)
    del owner._auto_time
    assert owner.status()['schedule'] == ['23:58']
    assert owner.status()['next_run_at'] == '2026-11-11T23:58:00+08:00'


def test_notification_only_automatic_real_changes(tmp_path, monkeypatch):
    owner, _, writer, _ = worker(tmp_path)
    original = owner.writer
    def write(*args):
        return original(*args) | {'changed_count': 1, 'changed_metrics': ['heat']}
    owner.writer = write
    emitted = []
    monkeypatch.setattr('app.services.market_collection.emit_collection_result', lambda *a, **kw: emitted.append(kw))
    assert owner.tick(), str(owner.status())
    assert len(emitted) == 1 and emitted[0]['changed_count'] > 0
    assert emitted[0]['scheduled'] is True
    assert not owner.tick(EVENING + timedelta(seconds=30))
    assert len(emitted) == 1


def test_no_change_does_not_notify(tmp_path, monkeypatch):
    owner, _, _, _ = worker(tmp_path)
    emitted = []
    monkeypatch.setattr('app.services.market_collection.emit_collection_result', lambda *a, **kw: emitted.append(kw))
    owner.tick()
    assert not emitted


def test_dynamic_schedule_changes_scope_and_next_run(tmp_path, monkeypatch):
    owner, _, _, _ = worker(tmp_path)
    del owner._auto_time
    monkeypatch.setattr('app.services.market_collection.task_config', lambda settings, key: {'times': ['01:30'], 'enabled': True})
    now = datetime(2026, 11, 11, 1, 29, tzinfo=CHINA)
    assert owner._scheduled_scope(now) == 'scheduled:2026-11-10'
    assert owner._scheduled_scope(now + timedelta(minutes=1)) == 'scheduled:2026-11-11'
    assert owner.status()['next_run_at'] == '2026-11-11T01:30:00+08:00'


@pytest.mark.parametrize('notify_enabled,changed_count', [(True, 1), (False, 1), (True, 0)])
def test_manual_run_notifies_only_when_enabled_and_business_changed(tmp_path, monkeypatch, notify_enabled, changed_count):
    from app.services import collection_notifications as notifications
    monkeypatch.setattr(notifications, 'enabled', lambda settings, task: task == 'market' and notify_enabled)
    monkeypatch.setattr(notifications, 'start_notifications', lambda *_: None)
    owner, _, _, _ = worker(tmp_path)
    original = owner.writer
    owner.writer = lambda *args: original(*args) | {'changed_count': changed_count, 'changed_metrics': ['heat']}
    owner.request_run('manual-notify-test')
    assert owner.tick(), str(owner.status())
    with notifications._database(owner.settings) as db:
        events = db.execute('SELECT task,run,message FROM outbox').fetchall()
    if notify_enabled and changed_count:
        assert len(events) == 1
        assert events[0]['task'] == 'market'
        assert events[0]['run'] == 'manual-notify-test'
        assert '更新记录：1' in events[0]['message']
    else:
        assert not events
    # Replaying the same manual request must never enqueue another notification.
    owner.request_run('manual-notify-test')
    owner.tick()
    with notifications._database(owner.settings) as db:
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == len(events)


@pytest.mark.parametrize('moment, expected', [
    ('2026-11-13T23:59:00+08:00', '2026-11-16T23:58:00+08:00'),
    ('2026-09-30T23:59:00+08:00', '2026-10-08T23:58:00+08:00'),
    ('2026-12-31T23:59:00+08:00', None),
])
@pytest.mark.parametrize('existing_ledger', [False, True])
def test_next_run_skips_closed_dates_and_stops_at_unknown_calendar(tmp_path, moment, expected, existing_ledger):
    owner, _, _, clock = worker(tmp_path)
    del owner._auto_time
    clock.value = datetime.fromisoformat(moment)
    if existing_ledger:
        with owner.connect():
            pass
    assert owner.status()['next_run_at'] == expected
