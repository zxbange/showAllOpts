"""Synthetic vendor fixtures; real MySQL is never contacted."""
from contextlib import nullcontext
from datetime import date, datetime, timedelta
import json
import re

import httpx
import pytest
from sqlalchemy import create_engine, event, select, text

from app.core.config import Settings
from app.services import index_valuation_collection as collection
from app.services.index_valuation_rules import INDEXES, PUBLIC_SOURCE, SOURCE, summarize
from app.services.index_valuation_source import EastmoneySource
from app.services.valuation_ruler import ValuationRuler
from app.services.valuation_ruler_model import ValuationError

DAY = date(2026, 11, 13)
MOMENT = datetime.fromisoformat('2026-11-13T18:30:00+08:00')


@pytest.fixture
def service(tmp_path, monkeypatch):
    monitor = ValuationRuler(Settings(_env_file=None, log_dir=tmp_path, valuation_ruler_worker_enabled=False))
    engine = create_engine('sqlite:///'+str(tmp_path/'source.sqlite3'))
    monkeypatch.setattr(collection, 'build_engine', lambda *a, **k: engine)
    monkeypatch.setattr(collection, 'database_config', lambda *a: {'revision': 1})
    yield monitor, engine
    monitor.stop()
    engine.dispose()


def vendor(fault=None):
    requests = []
    def handle(request):
        p = request.url.params
        requests.append(dict(p))
        if request.url.path.endswith('kline/get'):
            return httpx.Response(200, json={'rc': 0, 'data': {'code': p['secid'][2:], 'market': 1,
                'klines': ['2026-11-12,4000,4010,4020,3990,100', '2026-11-13,4010,4020,4030,4000,100']}})
        report = p['reportName']
        if report == 'RPT_INDEX_TS_COMPONENT':
            kind = p['filter'].split('"')[1]
            codes = {'1': range(1, 301), '2': range(1, 51), '7': range(301, 1301)}[kind]
            rows = [dict(SECUCODE=f'{i:06d}.SZ', SECURITY_CODE=f'{i:06d}', TYPE=kind,
                         MAXTRADEDATE='2026-11-13 00:00:00') for i in codes]
            if fault == 'old_roster': rows[0]['MAXTRADEDATE'] = '2026-11-12 00:00:00'
            if fault == 'duplicate_roster': rows[-1] = rows[0]
        else:
            codes = re.findall(r'"(\d{6})"', p['filter'])
            assert "(TRADE_DATE='2026-11-13')" in p['filter']
            rows = [dict(SECUCODE=c+'.SZ', SECURITY_CODE=c, TRADE_DATE='2026-11-13 00:00:00',
                         PE_TTM=10, PE_LAR=20, TOTAL_MARKET_CAP=10_000_000_000) for c in codes]
            if fault == 'null_pe': rows[0]['PE_TTM'] = None
            if fault == 'zero_pe': rows[0]['PE_TTM'] = 0
            if fault == 'bad_date': rows[0]['TRADE_DATE'] = '2026-11-12 00:00:00'
            if fault == 'duplicate_values': rows[-1] = rows[0]
            if fault == 'missing_cap': rows[0]['TOTAL_MARKET_CAP'] = None
            if fault == 'infinite': rows[0]['PE_TTM'] = 'Infinity'
            if fault == 'missing_row': rows.pop()
        count, page = len(rows), int(p['pageNumber'])
        data = rows[(page-1)*500:page*500]
        if fault == 'truncated_page' and count == 1000: data = data[:100]
        if fault == 'business_error': return httpx.Response(200, json={'success': False, 'result': None})
        return httpx.Response(200, json={'success': True, 'result': dict(count=count, pages=(count+499)//500, data=data)})
    return httpx.Client(transport=httpx.MockTransport(handle)), requests


def attach(monitor, fault=None):
    client, calls = vendor(fault)
    monitor.client_factory = lambda: nullcontext(client)
    return calls


def test_all_three_indices_complete_pagination_shared_values_and_both_stores(service):
    monitor, engine = service
    calls = attach(monitor)
    assert monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'idle', monitor.read_state()
    with engine.connect() as db:
        assert db.execute(select(collection.DAILY)).all().__len__() == 3
        assert db.execute(select(collection.MEMBER)).all().__len__() == 3
        assert db.execute(select(collection.HEAD.c.revision)).scalar_one() == 1
    for code in INDEXES:
        history = monitor.history(MOMENT, index_code=code, basis='lyr')
        assert history['sources'] == [PUBLIC_SOURCE]
        assert history['latest']['overall_pe'] == history['latest']['mean_pe'] == history['latest']['median_pe'] == 20
    assert len([p for p in calls if p.get('reportName') == 'RPT_VALUEANALYSIS_DET']) == 7
    assert any(p.get('pageNumber') == '2' and p.get('filter') == '(TYPE="7")' for p in calls)
    with monitor.db() as db: assert db.execute('SELECT count(*) FROM index_rosters_v2').fetchone()[0] == 0
    # A repeated request must not rewrite the source or generate another revision.
    attach(monitor); monitor.refresh(background=False, moment=MOMENT)
    with engine.connect() as db: assert db.execute(select(collection.HEAD.c.revision)).scalar_one() == 1


@pytest.mark.parametrize('fault', ['old_roster', 'duplicate_roster', 'truncated_page', 'business_error',
    'bad_date', 'duplicate_values', 'missing_cap', 'infinite', 'missing_row'])
def test_incomplete_vendor_never_publishes_partial_index_day(service, fault):
    monitor, _ = service
    attach(monitor, fault); monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'failed'
    assert monitor.history(MOMENT, index_code='000852.XSHG')['points'] == []


@pytest.mark.parametrize('fault', ['null_pe', 'zero_pe'])
def test_disclosed_unavailable_pe_keeps_members_but_not_whole_index_ratio(service, fault):
    monitor, _ = service
    attach(monitor, fault); monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'idle', monitor.read_state()
    latest = monitor.history(MOMENT)['latest']
    assert latest['member_count'] == 300 and latest['missing_pe_count'] == 2
    assert 'overall_pe' not in latest and latest['mean_pe'] == 10


def test_database_failure_rolls_back_and_retains_retry_roster(service):
    monitor, engine = service
    def fail(conn, cursor, statement, *a):
        if statement.startswith('INSERT INTO valuation_ruler_member_v2'): raise RuntimeError('synthetic private error')
    event.listen(engine, 'before_cursor_execute', fail)
    attach(monitor); monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'failed'
    assert 'synthetic private' not in monitor.status()['error']
    with engine.connect() as db: assert db.execute(select(collection.DAILY)).all() == []
    with monitor.db() as db: assert db.execute('SELECT count(*) FROM index_rosters_v2').fetchone()[0] == 1
    event.remove(engine, 'before_cursor_execute', fail)
    attach(monitor); monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'idle'


def test_preserves_jq_history_and_uses_actual_saved_source(service):
    monitor, engine = service
    client, _ = vendor()
    source = EastmoneySource(client, monitor, lambda: None)
    records = source.collect_latest(DAY, MOMENT)
    jq = [summarize(d['trade_date'], d['index_close'], json.loads(r['payload']), d['index_code'], source=SOURCE) for d, r in records]
    collection.initialize(engine)
    with engine.begin() as db:
        for daily, raw in jq:
            db.execute(collection.DAILY.insert().values(**daily))
            db.execute(collection.MEMBER.insert().values(**raw))
    attach(monitor); monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'idle', monitor.read_state()
    assert monitor.history(MOMENT)['sources'] == [SOURCE]
    with engine.connect() as db: assert db.execute(select(collection.HEAD.c.revision)).scalar_one() == 0


def test_local_failure_after_mysql_commit_recovers_without_duplicate_writes(service, monkeypatch):
    monitor, engine = service
    real = collection.init_store
    def fail(db): raise RuntimeError('synthetic disk unavailable')
    monkeypatch.setattr(collection, 'init_store', fail)
    attach(monitor); monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'failed'
    assert '数据库已保存' in monitor.status()['error']
    with engine.connect() as db:
        assert len(db.execute(select(collection.DAILY)).all()) == 3
        assert db.execute(select(collection.HEAD.c.revision)).scalar_one() == 1
    monkeypatch.setattr(collection, 'init_store', real)
    attach(monitor); monitor.refresh(background=False, moment=MOMENT)
    assert monitor.status()['status'] == 'idle'
    assert monitor.history(MOMENT)['latest']['source'] == PUBLIC_SOURCE
    with engine.connect() as db: assert db.execute(select(collection.HEAD.c.revision)).scalar_one() == 1


def test_default_1830_and_failed_slots_retry_after_15_minutes(service):
    monitor, _ = service
    monitor.settings.valuation_ruler_worker_enabled = True
    calls = attach(monitor, 'business_error')
    monitor.tick(MOMENT, background=False)
    n = len(calls)
    assert not monitor.tick(MOMENT+timedelta(minutes=14), background=False)
    assert len(calls) == n
    attach(monitor)
    assert monitor.tick(MOMENT+timedelta(minutes=16), background=False)
    assert monitor.status()['status'] == 'idle'
    assert not monitor.tick(MOMENT+timedelta(minutes=17), background=False)
    assert monitor.status()['schedule'] == '18:30 Asia/Shanghai'
