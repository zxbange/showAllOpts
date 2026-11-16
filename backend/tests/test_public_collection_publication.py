"""External sources are authoritative; database/local failures remain recoverable."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.models.cash_cow import CashCowSnapshot
from app.services.cash_cow import CashCowScreen
from app.services.cash_cow_import import PUBLIC_SOURCE, validate_snapshot, write_public
from app.services.public_publication import PublicPublication
from app.services import growth_dividend_import as growth
from test_cash_cow import Source, run


@pytest.fixture
def screen(tmp_path):
    return CashCowScreen(Settings(_env_file=None, log_dir=tmp_path / 'log'), years=3,
                         source_factory=Source, client_factory=lambda: nullcontext(None))


def test_cash_cow_publishes_same_complete_payload_to_both_stores(screen, public_result_database):
    run(screen)
    with public_result_database.connect() as conn:
        validate_snapshot(conn)
        payload = conn.execute(select(CashCowSnapshot.payload)).scalar_one()
    assert set(payload) == {'3', '5', '10'}
    with screen.db() as db:
        assert {str(year): json.loads(raw) for year, raw in db.execute('SELECT * FROM snapshots')} == payload
    assert all(value['snapshot']['source'] == PUBLIC_SOURCE for value in payload.values())
    cache = SnapshotCache(screen.settings, public_result_database)
    cache.root.mkdir(parents=True, exist_ok=True)
    filename, count = cache.copy_module('cash-cow')
    cache.write_state({'modules': {'cash-cow': {'file': filename, 'updated_at': '2026-11-13'}}})
    assert count == 1
    restored = CashCowScreen(Settings(_env_file=None, log_dir=screen.root / 'other'), years=3, cache=cache)
    assert restored.latest()['snapshot'] == payload['3']['snapshot']


def test_cash_database_failure_retries_saved_result_without_refetch(screen, public_result_database, monkeypatch):
    calls = []
    class Counted(Source):
        def universe(self):
            calls.append(1)
            return super().universe()
    screen.source_factory = Counted
    def fail(conn, cursor, sql, *args):
        if sql.startswith('INSERT INTO cash_cow_snapshot'):
            raise RuntimeError('synthetic database unavailable')
    event.listen(public_result_database, 'before_cursor_execute', fail)
    run(screen)
    assert screen.latest()['snapshot'] is None and screen.status()['error']
    event.remove(public_result_database, 'before_cursor_execute', fail)
    run(screen)
    assert calls == [1]
    assert screen.latest()['snapshot']['source'] == PUBLIC_SOURCE
    with screen.db() as db:
        assert db.execute('SELECT count(*) FROM public_publication').fetchone()[0] == 0


@pytest.mark.parametrize('corruption', ['missing_period', 'counts', 'profit', 'average', 'run', 'legacy'])
def test_cash_invalid_or_legacy_snapshot_cannot_replace_external(screen, public_result_database, corruption):
    run(screen)
    with public_result_database.connect() as conn:
        original = conn.execute(select(CashCowSnapshot.payload)).scalar_one()
    data = deepcopy(original)
    if corruption == 'missing_period': del data['5']
    elif corruption == 'counts': data['3']['snapshot']['matched_count'] = 4
    elif corruption == 'profit': data['3']['items'][0]['years'][0]['adjusted_profit'] = -1
    elif corruption == 'average': data['3']['items'][0]['average_yield'] = 12
    elif corruption == 'legacy': data['3']['snapshot']['source'] = '聚宽'
    else: data['3']['snapshot']['run_id'] = 'different'
    with pytest.raises(ValueError):
        write_public(public_result_database, data, lambda: None)
    with public_result_database.connect() as conn:
        assert conn.execute(select(CashCowSnapshot.payload)).scalar_one() == original


def test_remote_commit_then_local_failure_replays_idempotently(screen, public_result_database):
    run(screen)
    with public_result_database.connect() as conn:
        payload = conn.execute(select(CashCowSnapshot.payload)).scalar_one()
    publication = PublicPublication(screen)
    try:
        publication.stage(payload)
        def fail(data, db):
            db.execute('DELETE FROM snapshots')
            raise RuntimeError('disk interruption')
        with pytest.raises(RuntimeError):
            publication.resume(write_public, fail, lambda: None)
        assert screen.latest()['snapshot'] == payload['3']['snapshot']
        observed = []
        assert publication.resume(write_public, lambda data, db: observed.append(data), lambda: None)
        assert observed == [payload]
    finally:
        publication.close()


def test_old_pending_snapshot_does_not_replace_this_months_collection(screen, public_result_database):
    run(screen)
    with public_result_database.connect() as conn:
        old = conn.execute(select(CashCowSnapshot.payload)).scalar_one()
    publication = PublicPublication(screen)
    try:
        publication.stage(old)
    finally:
        publication.close()
    calls = []
    class Counted(Source):
        def universe(self):
            calls.append(1)
            return super().universe()
    screen.source_factory = Counted
    assert screen.refresh(background=False, moment=datetime(2026, 11, 11, tzinfo=timezone.utc), slot='2026-11-11')
    assert calls == [1]
    assert screen.latest()['snapshot']['as_of'] == '2026-11-11'
    assert screen.read_state()['scheduled_success_slot'] == '2026-11-11'


def test_growth_public_replaces_legacy_persists_and_replays(public_result_database):
    engine = public_result_database
    day = date(2026, 11, 10)
    growth.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(growth.CALENDAR.insert(), dict(trade_date=day, source=growth.LEGACY_CALENDAR_SOURCE,
                                                  updated_at=datetime(2026, 11, 10)))
        conn.execute(growth.PRICES.insert(), dict(index_code='000922.XSHG', trade_date=day, close=1,
                    source=growth.LEGACY_SOURCE, updated_at=datetime(2026, 11, 11)))
    payload = dict(points=[['000922', str(day), 5000, '腾讯财经', '', '2026-11-10T18:00:00+08:00']])
    for _ in range(2):
        assert growth.write_public(engine, payload, lambda: None) == payload
    days, rows = growth.read_source(engine, day, lambda: None)
    assert days == [day] and rows[0][2:4] == (5000, '腾讯财经')
    older = deepcopy(payload)
    older['points'][0][2] = 2
    older['points'][0][5] = '2026-11-09T18:00:00+08:00'
    assert growth.write_public(engine, older, lambda: None)['points'][0][2] == 5000


def test_growth_legacy_only_source_cannot_restore_old_results(public_result_database):
    engine = public_result_database
    growth.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(growth.CALENDAR.insert(), dict(trade_date=date(2026, 11, 10),
                     source=growth.LEGACY_CALENDAR_SOURCE, updated_at=datetime(2026, 11, 10)))
        conn.execute(growth.PRICES.insert(), dict(index_code='000922.XSHG', trade_date=date(2026, 11, 10),
                     close=1, source=growth.LEGACY_SOURCE, updated_at=datetime(2026, 11, 10)))
    with pytest.raises(ValueError, match='旧聚宽'):
        growth.read_source(engine, date(2026, 11, 10), lambda: None)


def test_pending_data_cannot_be_published_to_another_database(screen, monkeypatch):
    publication = PublicPublication(screen)
    try:
        publication.stage({'points': []})
        with screen.db() as db:
            db.execute("UPDATE public_publication SET target='another-database'")
        def unexpected(*args):
            pytest.fail('Changed target must be rejected before a database write')
        with pytest.raises(RuntimeError, match='另一个数据库'):
            publication.resume(unexpected, unexpected, lambda: None)
        with screen.db() as db:
            assert db.execute('SELECT count(*) FROM public_publication').fetchone()[0] == 1
    finally:
        publication.close()


def test_legacy_cached_cash_cow_cannot_supersede_public_result(screen, monkeypatch):
    run(screen)
    result = screen.latest()['snapshot']
    from app.services import cash_cow_import
    from types import SimpleNamespace
    older_source = {'snapshot': dict(result, source='聚宽', as_of='2099-12-31'), 'items': []}
    class Session:
        def get(self, *args):
            return SimpleNamespace(payload={'3': older_source})
    cache = SimpleNamespace(session=lambda _: nullcontext(Session()))
    assert cash_cow_import.latest_cached(cache, 3) is None
    screen.cache = cache
    assert screen.latest()['snapshot'] == result


def test_joinquant_entries_are_retired_without_removing_database_models():
    root = Path(__file__).resolve().parents[2]
    text = (root / 'collectors/策略批量采集_数据库版.py').read_text(encoding='utf8')
    assert 'def _growth_dividend_strategy' not in text
    assert 'def _cash_cow_strategy' not in text
    for filename in ('奶牛股选股_数据库版.py', '创红比历史补采_聚宽研究版.py'):
        assert not (root / 'collectors' / filename).exists()
    assert CashCowSnapshot.__tablename__ == 'cash_cow_snapshot'
    assert growth.PRICES.name == 'growth_dividend_index_history'
