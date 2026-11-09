"""Offline source-window integration: no production DB or live requests."""
from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest

from app.core.config import Settings
from app.services.cci_coal import CCICoal, latest_slot
from app.services.cci_source import GRADES, validate_rows


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import app.services.cci_coal as module
    import app.services.commodities as commodities
    clock = [datetime(2026, 11, 2, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(module, 'now', lambda: clock[0])
    monkeypatch.setattr(commodities, 'now', lambda: clock[0])
    monkeypatch.setattr(module, 'SEED', tmp_path / 'absent.json')
    settings = Settings(_env_file=None, log_dir=tmp_path)
    store = CCICoal(settings)
    return module, clock, settings, store


def seed(store, *pairs):
    rows = validate_rows([dict(quoteDate=d, grade=g, price=1000, change=None) for d, g in pairs], today=date(2027, 12, 31))
    with store.db() as db:
        db.executemany('INSERT INTO cci_quotes VALUES(?,?,?)',
                       [(r['quoteDate'], r['grade'], json.dumps(r, ensure_ascii=False)) for r in rows])


def install_source(monkeypatch, module, available, failures=None):
    calls = []
    real_client = httpx.Client
    def handle(request):
        p = request.url.params
        start, end, grade = p['date_from'], p['date_to'], p['index_name']
        calls.append((start, end, grade))
        assert 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days < 31
        if failures and failures(start, end, grade):
            raise httpx.ReadTimeout('private-token-must-not-leak', request=request)
        rows = [dict(kind='index', data=dict(date=d, index_type='日指数', index_name=g,
                                            price=1001, change=None, unit='元/吨'))
                for d, g in sorted(available) if g == grade and start <= d <= end]
        return httpx.Response(200, json=dict(rows=rows, total=len(rows)))
    monkeypatch.setattr(module.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    return calls


def pairs(store):
    return {(r['quoteDate'], r['grade']) for r in store.history()['rows']}


def test_each_grade_recovers_from_its_own_latest_quote_not_early_seed(setup, monkeypatch):
    module, clock, _, store = setup
    latest = dict(zip(GRADES, ['2026-11-02', '2026-11-04', '2026-11-06']))
    seed(store, ('2026-01-01', GRADES[0]), *[(d, g) for g, d in latest.items()])
    original = pairs(store)
    available = {(f'2026-11-{n:02}', g) for n in (2, 3, 4, 5, 6, 9) for g in GRADES}
    calls = install_source(monkeypatch, module, available)
    clock[0] = datetime(2026, 11, 9, 13, tzinfo=timezone.utc)
    assert store.refresh()  # Manual uses exactly the same recovery collector.
    store.thread.join(5)
    expected = {(d, g) for d, g in available if d >= latest[g]}
    assert pairs(store) == original | expected
    assert all(start >= latest[g] for start, _, g in calls)
    assert not any(d in {'2026-11-07', '2026-11-08'} for d, _ in pairs(store))
    state = store.status()
    assert state['outcome'] == 'completed' and state['targetHits'] == 3
    assert state['recovery']['pendingCount'] == state['recovery']['unavailableCount'] == 6
    assert state['recovery']['deferredCount'] == 0
    assert state['recovery']['requestCount'] == len(calls) == 6
    old = store.history()['rows']
    store.collect(store.lock())
    assert store.history()['rows'] == old
    assert store.status()['inserted'] == store.status()['changed'] == 0


def test_newer_success_does_not_hide_hole_and_restart_rechecks_empty_dates(setup, monkeypatch):
    module, clock, settings, store = setup
    seed(store, *[('2026-11-02', g) for g in GRADES])
    available = {(f'2026-11-{n:02}', g) for n in (3, 4, 5, 6) for g in GRADES}
    hole = ('2026-11-04', 'CCI5000')
    available.remove(hole)
    calls = install_source(monkeypatch, module, available)
    clock[0] = datetime(2026, 11, 6, 13, tzinfo=timezone.utc)
    store.collect(store.lock())
    assert hole not in pairs(store) and ('2026-11-06', 'CCI5000') in pairs(store)
    assert store.status()['recovery']['pendingCount'] == 1
    restarted = CCICoal(settings)
    available.add(hole)
    calls.clear()
    restarted.collect(restarted.lock())
    assert hole in pairs(restarted) and restarted.status()['inserted'] == 1
    assert restarted.status()['recovery']['pendingCount'] == 0
    assert [g for start, _, g in calls if start < '2026-11-06'] == ['CCI5000']


def test_no_success_uses_installation_day_not_arbitrary_full_history(setup, monkeypatch):
    module, clock, settings, store = setup
    calls = install_source(monkeypatch, module, set())
    clock[0] = datetime(2026, 11, 4, 16, 1, tzinfo=timezone.utc)  # Shanghai Nov 5
    store.collect(store.lock())
    assert pairs(store) == set()
    state = store.status()
    assert state['targetDate'] == '2026-11-05'
    assert state['recovery']['dateFrom'] == '2026-11-02'
    assert state['recovery']['pendingCount'] == state['recovery']['unavailableCount'] == 12
    assert all(row['origin'] == 'installation_date' for row in state['recovery']['initializedFrom'])
    assert min(start for start, _, _ in calls) == '2026-11-02'
    assert CCICoal(settings).recovery_plan('2026-11-06')[2]['dateFrom'] == '2026-11-02'


def test_network_failure_preserves_other_windows_and_retry_finishes(setup, monkeypatch, caplog):
    module, clock, _, store = setup
    seed(store, *[('2026-11-02', g) for g in GRADES])
    available = {(f'2026-11-{n:02}', g) for n in (3, 4, 5) for g in GRADES}
    fail = [True]
    calls = install_source(monkeypatch, module, available,
                           lambda start, end, grade: fail[0] and start < '2026-11-05' and grade == 'CCI5000')
    clock[0] = datetime(2026, 11, 5, 13, tzinfo=timezone.utc)
    store.collect(store.lock())
    assert store.status()['outcome'] == 'partial'
    assert store.status()['targetHits'] == 3
    assert store.status()['recovery']['pendingCount'] == 2
    assert store.status()['recovery']['unavailableCount'] == 0
    with store.db() as db:
        assert db.execute('SELECT DISTINCT result FROM cci_gap_checks').fetchall() == [('failed',)]
    assert 'private-token' not in caplog.text
    fail[0] = False
    store.collect(store.lock())
    assert available <= pairs(store)
    assert store.status()['inserted'] == 2 and store.status()['recovery']['pendingCount'] == 0
    assert all(start >= '2026-11-02' for start, _, _ in calls)


def test_request_budget_continues_without_starving_later_windows(setup, monkeypatch):
    module, clock, settings, store = setup
    seed(store, *[('2026-11-02', g) for g in GRADES])
    available = {('2027-02-01', g) for g in GRADES}
    calls = install_source(monkeypatch, module, available)
    clock[0] = datetime(2027, 2, 2, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(CCICoal, 'max_requests', 4)  # Three today + one historical window.
    store.collect(store.lock())
    assert len(calls) == 4
    assert store.status()['recovery']['deferredCount'] > 0
    first_history = calls[-1]
    # Every run sees empty holidays in the earliest window, but rotates fairly
    # through unchecked windows and persists that progress across restarts.
    history_requests = []
    for i in range(9):
        clock[0] += timedelta(minutes=1)
        restarted = CCICoal(settings)
        calls.clear()
        restarted.collect(restarted.lock())
        assert len(calls) == 4
        history_requests.append(calls[-1])
    assert first_history not in history_requests[:8]
    assert available <= pairs(restarted)


def test_scheduled_21_update_recovers_outage_with_persisted_quota(setup, monkeypatch):
    module, clock, settings, store = setup
    seed(store, *[('2026-11-02', g) for g in GRADES])
    available = {(f'2026-11-{n:02}', g) for n in (3, 4, 5) for g in GRADES}
    calls = install_source(monkeypatch, module, available)
    clock[0] = datetime(2026, 11, 5, 13, tzinfo=timezone.utc)
    assert store.refresh(slot=latest_slot(clock[0]))
    store.thread.join(5)
    assert available <= pairs(store) and store.status()['outcome'] == 'completed'
    assert len(calls) == 6
    assert not CCICoal(settings).refresh(slot=latest_slot(clock[0]))


def test_deadline_and_stop_leave_deferred_gaps_and_release_lock(setup, monkeypatch):
    module, clock, _, store = setup
    seed(store, *[('2026-11-02', g) for g in GRADES])
    calls = install_source(monkeypatch, module, set())
    clock[0] = datetime(2026, 11, 5, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(store, 'max_collection_seconds', 0)
    store.collect(store.lock())
    assert not calls and not store.status()['running']
    assert store.status()['recovery']['deferredCount'] == 9
    store.stopping.set()
    store.collect(store.lock())
    assert store.status()['outcome'] == 'interrupted' and not store.status()['running']


def test_deadline_between_today_grades_commits_verified_rows_and_defers_unsent(setup, monkeypatch):
    module, clock, _, store = setup
    seed(store, *[('2026-11-02', g) for g in GRADES])
    calls = install_source(monkeypatch, module, {('2026-11-05', g) for g in GRADES})
    clock[0] = datetime(2026, 11, 5, 13, tzinfo=timezone.utc)
    ticks = iter([0, 0, 0, 0])  # Start, budget check, first grade, first response chunk.
    monkeypatch.setattr(module, 'monotonic', lambda: next(ticks, 121))
    store.collect(store.lock())
    state = store.status()
    assert len(calls) == state['recovery']['requestCount'] == 1
    assert state['targetHits'] == state['inserted'] == 1
    assert state['outcome'] == 'partial'
    assert ('2026-11-05', 'CCI5500') in pairs(store)
    assert state['recovery']['checkedCount'] == 1
    assert state['recovery']['pendingCount'] == state['recovery']['deferredCount'] == 8
    assert state['recovery']['unavailableCount'] == 0
    assert not state['running']
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM cci_gap_checks').fetchone()[0] == 0
