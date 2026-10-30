"""Clock edits do not access MySQL or trigger an immediate/repeated sync."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os

from fastapi.testclient import TestClient
import pytest

from app.core.cache import CHINA, MODULES, SnapshotCache
from app.core.cache_schedule import CacheScheduleError
from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def scheduled(tmp_path, engine, monkeypatch):
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=True,
                        cache_auto_refresh=False)
    cache = SnapshotCache(settings, engine)
    clock = [datetime(2026, 10, 29, 6, tzinfo=CHINA)]
    monkeypatch.setattr('app.core.cache.now', lambda: clock[0])
    calls = []
    def claimed(lock, modules, reason, slot=None, full=False):
        calls.append((modules, reason, slot, full))
        lock.release()
    monkeypatch.setattr(SnapshotCache, '_refresh_locked', lambda self, *a, **kw: claimed(*a, **kw))
    return settings, cache, clock, calls


def test_default_and_legacy_migration_preserve_saved_preference(scheduled, engine):
    settings, cache, clock, calls = scheduled
    assert cache.status()['daily_time'] == '07:00'
    old = SnapshotCache(settings.model_copy(update={'cache_refresh_time': '20:00'}), engine)
    assert old.status()['daily_time'] == '07:00'
    custom = SnapshotCache(settings.model_copy(update={'cache_refresh_time': '18:30'}), engine)
    assert custom.status()['daily_time'] == '18:30'
    cache.save_schedule('20:00')
    assert old.status()['daily_time'] == custom.status()['daily_time'] == '20:00'
    assert SnapshotCache(settings, engine).status()['daily_time'] == '20:00'
    assert calls == []


@pytest.mark.parametrize('value', ['00:00', '07:00', '23:59'])
def test_valid_boundaries_persist(scheduled, value):
    _, cache, _, calls = scheduled
    assert cache.save_schedule(value)['daily_time'] == value
    assert json.loads(cache.schedule.path.read_text())['daily_time'] == value
    assert calls == []


@pytest.mark.parametrize('value', ['', '7:00', '24:00', '12:60', '07:00:00', '07:00\n', '０７:００', None, 700])
def test_invalid_values_rejected_by_api(scheduled, engine, value):
    settings, cache, _, calls = scheduled
    with TestClient(create_app(settings, engine)) as client:
        result = client.post('/api/v1/cache/schedule', json={'daily_time': value})
        assert result.status_code == 422
    assert not cache.schedule.path.exists() and calls == []


def test_api_round_trip_and_disabled_auto(scheduled, engine):
    settings, cache, _, calls = scheduled
    with TestClient(create_app(settings, engine)) as client:
        result = client.post('/api/v1/cache/schedule', json={'daily_time': '08:15'})
        assert result.status_code == 200
        assert result.json() == {'daily_time': '08:15', 'timezone': 'Asia/Shanghai', 'next_refresh_at': None}
        status = client.get('/api/v1/cache/status').json()
        assert status['daily_time'] == '08:15' and not status['auto_refresh']
    assert cache.status()['daily_time'] == '08:15' and calls == []


def test_save_failure_keeps_old_preference_and_reports_safe_error(scheduled, engine, monkeypatch):
    settings, cache, _, _ = scheduled
    cache.save_schedule('08:15')
    def fail(*args):
        raise OSError('private/path/secret')
    monkeypatch.setattr(os, 'replace', fail)
    with TestClient(create_app(settings, engine)) as client:
        response = client.post('/api/v1/cache/schedule', json={'daily_time': '09:30'})
        assert response.status_code == 503
        assert 'secret' not in response.text
        assert '无法读写同步时间设置' in response.json()['detail']
    assert cache.status()['daily_time'] == '08:15'
    assert list(cache.schedule.root.glob('schedule-*.tmp')) == []


def test_corrupt_preference_does_not_silently_reset_or_leak_refresh_lock(scheduled):
    _, cache, _, _ = scheduled
    cache.schedule.root.mkdir(parents=True, exist_ok=True)
    cache.schedule.path.write_text('{}')
    with pytest.raises(CacheScheduleError):
        cache.refresh(background=False)
    with pytest.raises(CacheScheduleError):
        cache.tick()
    assert not cache.refreshing()


def test_change_before_due_shared_workers_and_once_per_day(scheduled, engine):
    settings, cache, clock, calls = scheduled
    other = SnapshotCache(settings, engine)
    cache.tick(); other.tick()
    clock[0] = clock[0].replace(hour=6, minute=30)
    cache.save_schedule('08:00')
    assert other.status()['daily_time'] == '08:00'
    cache.tick(); other.tick()
    clock[0] = clock[0].replace(hour=7)
    cache.tick(); other.tick()
    assert calls == []
    clock[0] = clock[0].replace(hour=8, minute=0)
    cache.tick(); other.tick()
    assert len(calls) == 1 and calls[0][0] == list(MODULES)
    cache.save_schedule('09:00')
    clock[0] = clock[0].replace(hour=9)
    cache.tick(); other.tick()
    assert len(calls) == 1  # Clock edit cannot create another daily slot.
    clock[0] += timedelta(days=1)
    other.tick(); cache.tick()
    assert len(calls) == 2


def test_worker_observes_change_after_due_without_losing_new_plan(scheduled, engine):
    settings, cache, clock, calls = scheduled
    other = SnapshotCache(settings, engine)
    other.tick()
    cache.save_schedule('06:01')
    clock[0] = clock[0].replace(minute=1)
    other.tick()
    assert len(calls) == 1


def test_past_or_exact_time_waits_until_next_day_and_restart_never_catches_up(scheduled, engine):
    settings, cache, clock, calls = scheduled
    cache.tick()
    cache.save_schedule('06:00')
    cache.tick()
    clock[0] = clock[0].replace(hour=8)
    cache.tick()
    restarted = SnapshotCache(settings, engine)
    restarted.tick()
    assert calls == []
    clock[0] = (clock[0] + timedelta(days=1)).replace(hour=6)
    restarted.tick(); cache.tick()
    assert len(calls) == 1


def test_midnight_and_utc_boundary(scheduled):
    _, cache, clock, calls = scheduled
    cache.tick()
    cache.save_schedule('00:00')
    clock[0] = datetime(2026, 10, 29, 15, 59, 59, tzinfo=timezone.utc)
    cache.tick()
    assert calls == []
    clock[0] += timedelta(seconds=1)
    cache.tick()
    assert len(calls) == 1 and calls[0][2] == '2026-10-30T00:00:00+08:00'


def test_busy_refresh_lock_does_not_block_save_or_lose_due_slot(scheduled, engine):
    settings, cache, clock, calls = scheduled
    other = SnapshotCache(settings, engine)
    cache.tick(); other.tick()
    lock = cache.acquire()
    try:
        other.save_schedule('08:00')
        clock[0] = clock[0].replace(hour=8)
        other.tick()
        assert calls == []
    finally:
        lock.release()
    other.tick()
    assert len(calls) == 1


def test_concurrent_saves_atomic_and_idempotent(scheduled, engine):
    settings, cache, clock, calls = scheduled
    other = SnapshotCache(settings, engine)
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(item.save_schedule, time) for item, time in [(cache, '08:00'), (other, '09:00')]]
        for result in results:
            result.result()
    plan = cache.schedule.read()
    assert other.schedule.read() == plan and plan['daily_time'] in ('08:00', '09:00')
    clock[0] += timedelta(hours=1)
    cache.save_schedule(plan['daily_time'])
    assert cache.schedule.read() == plan and calls == []


def test_next_plan_uses_beijing_and_day_dedup(scheduled):
    _, cache, clock, calls = scheduled
    cache.settings.cache_auto_refresh = True
    assert cache.status()['next_refresh_at'] == '2026-10-29T07:00:00+08:00'
    cache.tick()
    clock[0] = clock[0].replace(hour=7)
    # The due slot stays visible while awaiting the next scheduler check/lock.
    assert cache.status()['next_refresh_at'] == '2026-10-29T07:00:00+08:00'
    cache.tick()
    cache.save_schedule('23:59')
    assert cache.status()['next_refresh_at'] == '2026-10-30T23:59:00+08:00'


def test_manual_all_cannot_erase_today_claim_and_active_sync_keeps_saved_plan(tmp_path, engine, monkeypatch):
    settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=True, cache_auto_refresh=False)
    cache = SnapshotCache(settings, engine)
    clock = [datetime(2026, 10, 29, 6, tzinfo=CHINA)]
    monkeypatch.setattr('app.core.cache.now', lambda: clock[0])
    cache.tick()
    state = cache.read_state()
    state['scheduled_slot'] = '2026-10-29T00:00:00+08:00'
    cache.write_state(state)
    calls = []
    def copy(module):
        calls.append(module)
        if len(calls) == 1:
            # Saving during an actual refresh must not wait on refresh.lock,
            # and its independent file must survive publication of state.json.
            cache.save_schedule('08:00')
        return module + '-test.sqlite', 0
    monkeypatch.setattr(cache, 'copy_module', copy)
    cache.refresh(background=False)
    assert calls == list(MODULES)
    assert cache.schedule.read()['daily_time'] == '08:00'
    assert cache.read_state()['scheduled_slot'] == '2026-10-29T00:00:00+08:00'
    clock[0] = clock[0].replace(hour=8)
    cache.tick()
    assert calls == list(MODULES)


def test_disabled_cache_rejects_schedule_write(scheduled, engine):
    settings, cache, _, _ = scheduled
    with TestClient(create_app(settings.model_copy(update={'cache_enabled': False}), engine)) as client:
        response = client.post('/api/v1/cache/schedule', json={'daily_time': '08:00'})
        assert response.status_code == 409
    assert not cache.schedule.path.exists()


def test_first_tick_busy_across_due_time_keeps_live_worker_slot(scheduled):
    _, cache, clock, calls = scheduled
    clock[0] = clock[0].replace(hour=6, minute=59)
    lock = cache.acquire()
    try:
        cache.tick()  # First observation must count even though this lock is busy.
        clock[0] = clock[0].replace(hour=7, minute=1)
        cache.tick()
        assert calls == []
    finally:
        lock.release()
    cache.tick()
    assert len(calls) == 1 and calls[0][2] == '2026-10-29T07:00:00+08:00'
    cache.tick()
    assert len(calls) == 1


def test_first_tick_after_due_time_does_not_catch_up(scheduled):
    _, cache, clock, calls = scheduled
    clock[0] = clock[0].replace(hour=7, minute=1)
    cache.tick()
    assert calls == []
    clock[0] += timedelta(days=1)
    cache.tick()
    assert len(calls) == 1


def test_edit_during_first_busy_tick_uses_saved_plan_boundary(scheduled):
    _, cache, clock, calls = scheduled
    clock[0] = clock[0].replace(hour=6, minute=59)
    lock = cache.acquire()
    try:
        cache.tick()
        clock[0] = clock[0].replace(hour=7, minute=1)
        cache.save_schedule('07:00')  # An explicit edit after due must wait for tomorrow.
    finally:
        lock.release()
    cache.tick()
    assert calls == []
    clock[0] += timedelta(days=1)
    cache.tick()
    assert len(calls) == 1
