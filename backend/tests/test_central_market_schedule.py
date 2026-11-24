import pytest
from datetime import datetime, time, timedelta
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
    assert owner._data_cutoff(now.replace(hour=20), True) == DAY
    assert owner._scheduled_scope(now) == 'scheduled:v2:2026-11-11T00:01+08:00'
    assert owner._scheduled_scope(now - timedelta(seconds=1)) == 'scheduled:v2:2026-11-10T00:01+08:00'


def test_status_has_daily_next_slot_without_calendar(tmp_path):
    owner, _, _, _ = worker(tmp_path)
    del owner._auto_time
    assert owner.status()['schedule'] == ['23:58']
    assert owner.status()['next_run_at'] == '2026-11-11T23:58:00+08:00'


def test_automatic_notification_covers_real_changes(tmp_path, monkeypatch):
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


def test_no_change_notifies_if_an_automatic_run_actually_executed(tmp_path, monkeypatch):
    owner, _, _, _ = worker(tmp_path)
    emitted = []
    monkeypatch.setattr('app.services.market_collection.emit_collection_result', lambda *a, **kw: emitted.append(kw))
    owner.tick()
    assert len(emitted) == 1 and emitted[0]['scheduled'] is True
    assert emitted[0]['changed_count'] == 0
    owner.tick()
    assert len(emitted) == 1


def test_dynamic_schedule_changes_scope_and_next_run(tmp_path, monkeypatch):
    owner, _, _, _ = worker(tmp_path)
    del owner._auto_time
    monkeypatch.setattr('app.services.market_collection.task_config', lambda settings, key: {'times': ['01:30'], 'enabled': True})
    now = datetime(2026, 11, 11, 1, 29, tzinfo=CHINA)
    assert owner._scheduled_scope(now) == 'scheduled:v2:2026-11-10T01:30+08:00'
    assert owner._scheduled_scope(now + timedelta(minutes=1)) == 'scheduled:v2:2026-11-11T01:30+08:00'
    assert owner.status()['next_run_at'] == '2026-11-11T01:30:00+08:00'


@pytest.mark.parametrize('notify_enabled,changed_count', [(True, 1), (False, 1), (True, 0)])
def test_manual_run_notifies_when_enabled_with_or_without_changes(tmp_path, monkeypatch, notify_enabled, changed_count):
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
    if notify_enabled:
        assert len(events) == 1
        assert events[0]['task'] == 'market'
        assert events[0]['run'] == 'manual-notify-test'
        assert f'更新记录：{changed_count}' in events[0]['message']
    else:
        assert not events
    # Replaying the same manual request must never enqueue another notification.
    owner.request_run('manual-notify-test')
    owner.tick()
    with notifications._database(owner.settings) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE run='manual-notify-test'").fetchone()[0] == len(events)


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


def test_manual_queue_keeps_admission_slot_and_does_not_claim_later_auto_slot(tmp_path):
    request = datetime(2026, 11, 11, 23, 57, tzinfo=CHINA)
    owner, provider, writer, clock = worker(tmp_path, moment=request, schedule=time(23, 58))
    owner.request_run('before-due-slot')
    clock.value = request + timedelta(minutes=2)
    assert owner.tick()
    assert provider.universe_calls == [DAY]
    assert owner._day(request.date()) is None
    assert owner._meta('planned_scope') != owner._scheduled_scope(clock())
    assert owner.tick()
    assert provider.universe_calls == [DAY, request.date()]


def test_manual_queue_preserves_old_schedule_through_config_change_and_restart(tmp_path):
    request = datetime(2026, 11, 11, 20, 55, tzinfo=CHINA)
    owner, _, _, _ = worker(tmp_path, moment=request, schedule=time(23, 58))
    owner.request_run('admitted-before-config-update')
    restart, provider, _, _ = worker(tmp_path, moment=request + timedelta(minutes=5), schedule=time(20, 0))
    assert restart.tick()
    assert provider.universe_calls == [DAY]
    assert restart._day(request.date()) is None
    with restart.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_run_clocks').fetchone()[0] == 0
    assert restart.tick()
    assert provider.universe_calls == [DAY, request.date()]


def test_manual_legacy_queue_without_clock_remains_runnable(tmp_path):
    request = datetime(2026, 11, 11, 20, 55, tzinfo=CHINA)
    owner, provider, _, _ = worker(tmp_path, moment=request, schedule=time(20, 0))
    with owner.connect() as db:
        db.execute('INSERT INTO manual_runs(id,status,requested_at,updated_at) VALUES(?,?,?,?)',
                   ('legacy', 'queued', request.isoformat(), request.isoformat()))
    assert owner.tick()
    assert provider.universe_calls == [request.date()]
