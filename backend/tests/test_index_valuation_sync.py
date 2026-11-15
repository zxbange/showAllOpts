import datetime as dt
import json

import pytest
from sqlalchemy import event, text

from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.core.history_sync import service_for
from app.services import index_valuation
from test_index_valuation_collector import collector, run, DAYS


@pytest.fixture
def synced(collector, tmp_path, monkeypatch):
    c = collector
    run(c)
    settings = Settings(_env_file=None, log_dir=tmp_path/'log', cache_enabled=True,
                        cache_auto_refresh=False, valuation_ruler_worker_enabled=False)
    monkeypatch.setattr('app.core.cache.now', lambda: dt.datetime(2026, 1, 7, 22, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    cache = SnapshotCache(settings, c.engine)
    sql = []
    event.listen(c.engine, 'before_cursor_execute', lambda conn, cur, statement, params, ctx, many: sql.append(statement))
    cache.refresh('valuation-ruler', background=False)
    service = service_for(cache, 'valuation-ruler')
    return c, cache, service, sql


def test_summary_only_sync_and_static_ttm_are_separate(synced, monkeypatch):
    c, cache, service, sql = synced
    entry = next(r for r in cache.status()['modules'] if r['id'] == 'valuation-ruler')
    assert entry['ready'] and entry['progress']['status'] == 'complete'
    assert not any('valuation_ruler_member' in statement for statement in sql)
    for code, (_, count, _) in index_valuation.INDICES.items():
        ttm = service.history(index_code=code, basis='ttm')
        lyr = service.history(index_code=code, basis='lyr')
        assert len(ttm['points']) == len(lyr['points']) == 3
        assert ttm['latest']['mean_pe'] == 10
        assert lyr['latest']['mean_pe'] == 20
        assert lyr['latest']['mean_pe_ttm'] == 10  # static selection must not relabel raw TTM
        assert ttm['latest']['member_count'] == count
        assert lyr['benchmarks']['p50'] == 20
    from app.services.index_valuation_source import EastmoneySource
    assert service.source_factory is EastmoneySource
    before = service.history()
    # Public collection remains available after JQ sync; a failed source keeps all history.
    def unavailable(*args):
        raise ValueError('synthetic unavailable')
    monkeypatch.setattr(EastmoneySource, 'collect_latest', unavailable)
    assert service.refresh(background=False) is True
    assert service.status()['status'] == 'failed' and service.history() == before
    cache.refresh('valuation-ruler', background=False)
    assert len(service.history()['points']) == 3


def test_failed_summary_does_not_replace_published_cache(synced):
    c, cache, service, sql = synced
    before = service.history()
    with c.engine.begin() as db:
        db.execute(text('UPDATE valuation_ruler_daily_v2 SET mean_pe_ttm=301'))
    cache.refresh('valuation-ruler', background=False)
    assert service.history() == before
    entry = next(r for r in cache.status()['modules'] if r['id'] == 'valuation-ruler')
    assert entry['progress']['status'] == 'failed'


def test_increment_reads_three_recent_sessions_and_new_rows_only(synced):
    c, cache, service, sql = synced
    # Add two later sessions, then inspect the actual WHERE boundary on the next run.
    with c.engine.begin() as db:
        rows = db.execute(text('SELECT * FROM valuation_ruler_daily_v2 WHERE trade_date=:day'),
                          {'day': str(DAYS[-1])}).mappings().all()
        for day in (dt.date(2026, 1, 8), dt.date(2026, 1, 9)):
            for row in rows:
                values = dict(row, trade_date=day, updated_at=dt.datetime(2026, 1, 9, 22))
                # Text driver returns strings for dates; use reflected typed table via collector.
                values['index_close'] = 4000
                db.execute(c.m.ValuationDaily.__table__.insert(), values)
    index_valuation.sync(c.engine, service, dt.date(2026, 1, 9), lambda: None, lambda *args: None)
    seen = []
    def record(conn, cur, statement, params, ctx, many):
        if 'FROM valuation_ruler_daily_v2' in statement and 'count(' not in statement and 'min(' not in statement:
            seen.append((statement, params))
    event.listen(c.engine, 'before_cursor_execute', record)
    try:
        index_valuation.sync(c.engine, service, dt.date(2026, 1, 9), lambda: None, lambda *args: None)
    finally:
        event.remove(c.engine, 'before_cursor_execute', record)
    assert len(seen) == 3
    assert all('trade_date >=' in statement and '2026-01-07' in params for statement, params in seen)


@pytest.mark.parametrize('has_baseline', [False, True])
def test_latest_daily_point_does_not_hide_earlier_source_history(synced, has_baseline):
    c, cache, service, _ = synced
    with service.db() as db:
        db.execute('DELETE FROM index_points_v2 WHERE trade_date < ?', (str(DAYS[-1]),))
        if not has_baseline:
            db.execute('DELETE FROM index_sync_baseline_v2')
    assert len(service.history()['points']) == 1
    cache.refresh('valuation-ruler', background=False)
    for code in index_valuation.INDICES:
        assert len(service.history(index_code=code)['points']) == 3
    entry = next(r for r in cache.status()['modules'] if r['id'] == 'valuation-ruler')
    assert entry['ready'] and not entry['error']
    assert '新增 6 个指数日' in entry['sync_note']
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM index_sync_baseline_v2').fetchone()[0] == 3


@pytest.mark.parametrize('bad', ['counts', 'basis', 'null_mean', 'overall_partial', 'hash', 'source_basis'])
def test_invalid_contract_rejected(synced, bad):
    c, _, _, _ = synced
    with c.engine.connect() as db:
        row = dict(db.execute(c.m.ValuationDaily.__table__.select().limit(1)).mappings().one())
    stats = json.loads(row['statistics'])
    if bad == 'counts': stats['ttm']['eligible'] -= 1
    if bad == 'basis': stats['profit_basis'] = 'guessed'
    if bad == 'null_mean': row['mean_pe_ttm'] = None
    if bad == 'overall_partial': stats['lyr'].update(overall_valid=299, overall_missing=1)
    if bad == 'hash': row['members_sha256'] = 'bad'
    if bad == 'source_basis': row['source'] = index_valuation.PUBLIC_SOURCE
    row['statistics'] = json.dumps(stats)
    with pytest.raises(index_valuation.SnapshotDataError):
        index_valuation.checked(row, DAYS[-1])


def test_packed_mysql_page_roundtrip_precision_order_and_query_limit(synced):
    import struct
    import zlib
    from sqlalchemy.dialects import mysql
    c, _, _, _ = synced
    table = c.m.ValuationDaily.__table__
    with c.engine.connect() as connection:
        rows = connection.execute(table.select().where(table.c.index_code == '000300.XSHG')
                                  .order_by(table.c.trade_date)).mappings().all()
    values = [[str(row[name]) if row[name] is not None else None for name in table.c.keys()] for row in reversed(rows)]
    raw = json.dumps(values).encode()
    packet = struct.pack('<I', len(raw)) + zlib.compress(raw)
    decoded = index_valuation.unpack_page(packet, list(table.c.keys()))
    assert [index_valuation.checked(r, DAYS[-1]) for r in decoded] == [index_valuation.checked(r, DAYS[-1]) for r in rows]
    query = table.select().where(table.c.index_code == '000300.XSHG').order_by(table.c.trade_date).limit(500)
    sql = str(index_valuation.packed_query(query).compile(dialect=mysql.dialect()))
    assert 'compress(json_arrayagg(json_array(' in sql and 'LIMIT' in sql
    assert 'valuation_ruler_member' not in sql
    assert index_valuation.unpack_page(None, list(table.c.keys())) == []
    for bad in [b'bad', struct.pack('<I', 9*1024*1024)+b'x', packet[:-5]]:
        with pytest.raises(index_valuation.SnapshotDataError):
            index_valuation.unpack_page(bad, list(table.c.keys()))


def test_summary_progress_advances_with_pages_and_counts_rows(synced, monkeypatch):
    from app.core.sync_progress import reporter
    c, _, service, _ = synced
    updates = []
    progress = []
    token = reporter.set(lambda stage, rows=0, percent=None: updates.append(rows))
    try:
        summary = index_valuation.sync(c.engine, service, DAYS[-1], lambda: None,
            lambda stage, percent: progress.append((stage, percent)), full=True)
    finally:
        reporter.reset(token)
    assert sum(updates) == summary['processed'] == 9
    assert any('3/3' in stage for stage, _ in progress)
    assert progress[-1][1] == 89


def test_legacy_overall_column_is_not_downloaded_or_exposed(synced):
    c, cache, service, sql = synced
    with c.engine.begin() as db:
        db.execute(text('ALTER TABLE valuation_ruler_daily_v2 ADD COLUMN overall_pe_ttm NUMERIC'))
        db.execute(text('UPDATE valuation_ruler_daily_v2 SET overall_pe_ttm=999'))
    sql.clear()
    cache.refresh('valuation-ruler', background=False)
    reads = [s for s in sql if 'FROM valuation_ruler_daily_v2' in s and s.lstrip().startswith('SELECT')]
    assert reads and all('overall_pe_ttm' not in s for s in reads)
    for point in service.history(basis='ttm')['points']:
        assert 'overall_pe_ttm' not in point and 'overall_pe' not in point
        assert point['mean_pe'] == point['median_pe'] == 10
    assert service.history(basis='lyr')['latest']['overall_pe'] == 20
