"""Offline regressions for clock-qualified market schedule identities."""
from datetime import date, datetime, time, timedelta
import logging

import pytest

from app.services.market_collection import CHINA
from test_market_collection import worker, historical_provider


def stamp(day, clock):
    return datetime.fromisoformat(f'{day}T{clock}').replace(tzinfo=CHINA)


def scope(day, clock):
    return 'scheduled:v2:' + stamp(day, clock).isoformat(timespec='minutes')


def legacy(owner, day, requested_at, *, planned=True, budget=0):
    old = 'scheduled:' + day
    with owner.connect() as db:
        if planned:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('planned_scope', old))
        if budget:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('dependency-budget:' + old, str(budget)))
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('pass-budget:' + old + ':' + day, str(budget)))
        if requested_at:
            db.execute("INSERT INTO manual_runs(id,status,trigger,phase,requested_at,updated_at,day) VALUES(?,?,?,?,?,?,?)",
                       ('legacy-' + day, 'complete', 'scheduled', 'complete', requested_at.isoformat(), requested_at.isoformat(), day))
    return old


def test_scope_contains_pinned_clock_and_survives_midnight_restart(tmp_path):
    w, _, _, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:59'))
    w._auto_time = lambda: time(23, 58)
    expected = scope('2026-11-11', '23:58')
    assert w._scheduled_scope(clock()) == expected
    clock.value = stamp('2026-11-12', '00:01')
    assert w._scheduled_scope(clock()) == expected
    restarted, _, _, _ = worker(tmp_path, moment=clock())
    restarted._auto_time = lambda: time(23, 58)
    assert restarted._scheduled_scope(clock()) == expected
    restarted._auto_time = lambda: time(0, 1)
    assert restarted._scheduled_scope(clock()) == scope('2026-11-12', '00:01')


def test_legacy_midnight_complete_does_not_hide_new_same_day_evening_target(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:58'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    writer.dates = {kind: {'2026-11-10'} for kind in ('heat', 'crowding')}
    legacy(w, '2026-11-11', stamp('2026-11-11', '00:01'))
    assert w.tick()
    assert provider.universe_calls == [date(2026, 11, 11)]
    assert writer.dates['heat'] == {'2026-11-10', '2026-11-11'}
    assert w._meta('planned_scope') == scope('2026-11-11', '23:58')
    assert not w.tick(clock.advance(seconds=30))
    assert len(provider.universe_calls) == 1


def test_legacy_matching_evening_window_replans_without_rewriting_published_metrics(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:59'))
    w._auto_time = lambda: time(23, 58)
    writer.dates = {kind: {'2026-11-11'} for kind in ('heat', 'crowding')}
    legacy(w, '2026-11-11', stamp('2026-11-11', '23:58'))
    assert not w.tick()
    assert not provider.universe_calls and not writer.calls
    assert w._meta('planned_scope') == scope('2026-11-11', '23:58')
    restarted, provider2, _, _ = worker(tmp_path, moment=stamp('2026-11-12', '00:02'))
    restarted._auto_time = lambda: time(23, 58)
    assert not restarted.tick()
    assert not provider2.universe_calls


def test_legacy_matching_window_preserves_budgets_and_old_keys(tmp_path):
    w, provider, _, clock = worker(tmp_path, moment=stamp('2026-11-12', '00:02'))
    w._auto_time = lambda: time(23, 58)
    old = legacy(w, '2026-11-11', stamp('2026-11-11', '23:59'), budget=4)
    assert not w.tick()
    new = scope('2026-11-11', '23:58')
    assert w._meta('dependency-budget:' + new) == '4'
    assert w._meta('pass-budget:' + new + ':2026-11-11') == '4'
    assert w._meta('dependency-budget:' + old) == '4'
    assert not provider.universe_calls


def test_no_run_evidence_does_not_claim_old_plan_completed_new_slot(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:58'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    writer.dates = {kind: {'2026-11-10'} for kind in ('heat', 'crowding')}
    legacy(w, '2026-11-11', None)
    assert w.tick()
    assert provider.universe_calls == [date(2026, 11, 11)]


def test_midnight_can_start_latest_due_open_slot_without_pending_rows(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-12', '00:01'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    assert w.tick()
    assert provider.universe_calls == [date(2026, 11, 11)]
    assert w._meta('planned_scope') == scope('2026-11-11', '23:58')
    restarted, provider2, writer2, _ = worker(tmp_path, moment=clock())
    restarted._auto_time = lambda: time(23, 58)
    writer2.dates = writer.dates
    assert not restarted.tick()
    assert not provider2.universe_calls


@pytest.mark.parametrize('moment', ['2026-11-14T00:01', '2026-11-16T00:01', '2026-10-08T00:01'])
def test_neither_current_nor_due_closed_slot_runs(tmp_path, moment):
    w, provider, writer, clock = worker(tmp_path, moment=datetime.fromisoformat(moment).replace(tzinfo=CHINA))
    w._auto_time = lambda: time(23, 58)
    assert not w.tick()
    assert not provider.universe_calls and not writer.calls


def test_scope_migration_does_not_clear_source_access_cooldown(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:58'))
    w._auto_time = lambda: time(23, 58)
    legacy(w, '2026-11-11', stamp('2026-11-11', '00:01'))
    until = stamp('2026-11-12', '23:58').isoformat()
    w._put_meta(source_paused_until=until)
    assert not w.tick()
    assert w._meta('source_paused_until') == until
    assert not provider.universe_calls


def test_closed_manual_failure_remains_manual_only_after_scope_upgrade(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-14', '23:58'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    provider.failures = {'sh600003'}
    w.request_run('closed-manual')
    assert not w.tick()
    assert provider.quote_calls
    assert w._meta('manual-only:2026-11-13')
    with w.connect() as db:
        assert not db.execute("SELECT 1 FROM metadata WHERE key LIKE '%budget:scheduled:%'").fetchone()
    calls = len(provider.quote_calls)
    clock.value = stamp('2026-11-16', '00:01')
    assert not w.tick()
    assert len(provider.quote_calls) == calls


def test_scheduler_note_is_throttled_per_reason_and_scope(tmp_path, caplog, monkeypatch):
    w, _, _, clock = worker(tmp_path)
    first = scope('2026-11-11', '00:01')
    monotonic = [1000.0]
    monkeypatch.setattr('app.services.market_collection.monotonic_time.monotonic', lambda: monotonic[0])
    with caplog.at_level(logging.INFO, logger='app.services.market_collection'):
        w._scheduler_note('already_planned', clock(), first)
        monotonic[0] += 299
        w._scheduler_note('already_planned', clock() + timedelta(minutes=4, seconds=59), first)
        assert len(caplog.records) == 1
        monotonic[0] += 1
        w._scheduler_note('already_planned', clock() + timedelta(minutes=5), first)
        assert len(caplog.records) == 2
        w._scheduler_note('budget_exhausted', clock(), first)
        w._scheduler_note('already_planned', clock(), scope('2026-11-11', '23:58'))
        assert len(caplog.records) == 4


def test_old_dependency_failure_for_previous_target_cannot_block_new_target(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:58'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    writer.dates = {kind: {'2026-11-10'} for kind in ('heat', 'crowding')}
    old = legacy(w, '2026-11-11', stamp('2026-11-11', '00:01'), budget=4)
    # The old midnight batch was attempting only the previous target day.
    with w.connect() as db:
        db.execute("UPDATE manual_runs SET day='2026-11-10'")
        db.execute('DELETE FROM metadata WHERE key=?', ('pass-budget:' + old + ':2026-11-11',))
        db.execute('INSERT INTO metadata VALUES (?,?)', ('pass-budget:' + old + ':2026-11-10', '4'))
    assert w.tick()
    assert provider.universe_calls == [date(2026, 11, 11)]
    assert w._meta('pass-budget:' + scope('2026-11-11', '23:58') + ':2026-11-10') == '4'
    assert not w._meta('dependency-budget:' + scope('2026-11-11', '23:58'))


def test_scope_migration_uses_max_once_and_preserves_legacy_keys(tmp_path):
    w, _, _, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:59'))
    w._auto_time = lambda: time(23, 58)
    old = legacy(w, '2026-11-11', stamp('2026-11-11', '23:58'), budget=3)
    new = scope('2026-11-11', '23:58')
    with w.connect() as db:
        db.execute('INSERT INTO metadata VALUES (?,?)', ('pass-budget:' + new + ':2026-11-11', '4'))
    lock = w._acquire()
    try:
        w._migrate_schedule_scope(clock(), new)
        with w.connect() as db:
            first = dict(db.execute('SELECT key,value FROM metadata'))
        w._migrate_schedule_scope(clock(), new)
        with w.connect() as db:
            assert dict(db.execute('SELECT key,value FROM metadata')) == first
    finally:
        lock.release()
    assert first['pass-budget:' + new + ':2026-11-11'] == '4'
    assert first['dependency-budget:' + new] == '3'
    assert first['pass-budget:' + old + ':2026-11-11'] == '3'
    assert first['dependency-budget:' + old] == '3'


def test_busy_lock_polling_does_not_flood_scheduler_logs(tmp_path, caplog):
    w, provider, _, clock = worker(tmp_path)
    lock = w._acquire()
    try:
        with caplog.at_level(logging.INFO, logger='app.services.market_collection'):
            for _ in range(8):
                assert not w.tick()
        decisions = [record for record in caplog.records if 'Market scheduler decision=' in record.getMessage()]
        assert len(decisions) == 1
        assert not provider.universe_calls
    finally:
        lock.release()


def test_old_clock_afternoon_manual_budget_cannot_block_new_evening_target(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:58'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    writer.dates = {kind: {'2026-11-10'} for kind in ('heat', 'crowding')}
    old = legacy(w, '2026-11-11', stamp('2026-11-11', '20:00'), budget=4)
    with w.connect() as db:
        db.execute("UPDATE manual_runs SET trigger='manual' WHERE id='legacy-2026-11-11'")
    assert w.tick()
    new = scope('2026-11-11', '23:58')
    assert provider.universe_calls == [date(2026, 11, 11)]
    assert writer.dates['heat'] == {'2026-11-10', '2026-11-11'}
    assert w._meta('pass-budget:' + new + ':2026-11-11') == '1'
    assert not w._meta('dependency-budget:' + new)
    assert w._meta('pass-budget:' + old + ':2026-11-11') == '4'
    assert not w.tick(clock.advance(seconds=30))
    assert len(provider.universe_calls) == 1


def test_running_evening_batch_keeps_identity_across_open_midnight(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-11', '23:58'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    provider.on_quote = lambda symbol: setattr(clock, 'value', stamp('2026-11-12', '00:01'))
    assert w.tick()
    expected = scope('2026-11-11', '23:58')
    assert w._meta('planned_scope') == expected
    assert w._meta('pass-budget:' + expected + ':2026-11-11') == '1'
    calls = len(provider.quote_calls)
    assert not w.tick()
    assert len(provider.quote_calls) == calls



def test_scheduler_notes_bound_memory_under_concurrent_calls(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    w, _, _, clock = worker(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: w._scheduler_note('lock_busy', clock(), str(i)), range(200)))
    assert len(w._scheduler_notes) == 64



def test_midnight_catchup_does_not_resume_explicitly_cancelled_due_slot(tmp_path):
    w, provider, writer, clock = worker(tmp_path, moment=stamp('2026-11-12', '00:01'))
    w._auto_time = lambda: time(23, 58)
    historical_provider(provider)
    w._put_meta(manual_paused_on='2026-11-11')
    assert not w.tick()
    assert not provider.universe_calls
    clock.value = stamp('2026-11-12', '23:58')
    assert w.tick()
    assert provider.universe_calls == [date(2026, 11, 12)]
