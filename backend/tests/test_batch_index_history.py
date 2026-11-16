"""Run the embedded valuation collector against real SQLite tables and synthetic platform APIs."""
import datetime as dt
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import event, func, select, text

from test_collector_batch import CONFIG, load

ROOT = Path(__file__).resolve().parents[2]
DAYS = [dt.date(2026, 1, day) for day in (5, 6, 7)]


@pytest.fixture
def collectors(monkeypatch, engine):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    monkeypatch.setitem(sys.modules, 'mysql_connector', SimpleNamespace(
        get_database_config=lambda: dict(CONFIG), get_database_options=lambda: {},
        mysql_drivername=lambda _: 'mysql+pymysql', engine_options=lambda _: {}))
    batch = load(ROOT / 'collectors/策略批量采集_数据库版.py', 'batch_index_integration')
    connections, calls = [], []
    def connect(config):
        connections.append(dict(config))
        assert config == CONFIG
        return engine
    monkeypatch.setattr(batch, 'connect_database', connect)
    monkeypatch.setattr(engine, 'dispose', lambda: None)
    codes = ['{:06d}.XSHG'.format(600000+i) for i in range(300)]
    failure = {}
    def fundamentals(query, date):
        calls.append(('pe', date, len(query)))
        if failure.get('pe') == date:
            raise RuntimeError('synthetic PE provider failure')
        frame = pd.DataFrame({'code': codes, 'day': [date]*300,
                             'pe_ratio': [None, -2] + [10]*298,
                             'pe_ratio_lyr': [20]*300, 'market_cap': [100]*300})
        return frame[frame['code'].isin(query)].copy()
    def prices(code, start_date, end_date, **kwargs):
        assert kwargs['fq'] is None and kwargs['fields'] == ['close']
        calls.append(('price', code, start_date, end_date))
        dates = [day for day in DAYS if start_date <= day <= end_date and failure.get(code) != day]
        return pd.DataFrame({'close': [4000]*len(dates)}, index=dates)
    apis = dict(get_all_trade_days=lambda: DAYS,
                get_trade_days=lambda end_date, start_date=DAYS[0]: [d for d in DAYS if start_date <= d <= end_date],
                get_index_stocks=lambda index, date: codes,
                valuation=SimpleNamespace(code=SimpleNamespace(in_=lambda codes: codes), day=object(), pe_ratio=object(),
                                          pe_ratio_lyr=object(), market_cap=object()),
                query=lambda *args: SimpleNamespace(filter=lambda codes: codes),
                get_fundamentals=fundamentals, get_price=prices)
    options = {name: dict(start_date=str(DAYS[0]), end_date=str(DAYS[-1])) for name in ('估值标尺',)}
    options['估值标尺']['indices'] = ['000300.XSHG']
    return SimpleNamespace(batch=batch, apis=apis, options=options, calls=calls, failure=failure,
                           connections=connections, engine=engine)


def run(c, names=None):
    return c.batch.run_all(names or ['估值标尺'], options=c.options, db_config=CONFIG,
                           api_namespace=c.apis, preview_rows=0, raise_on_failure=False)


def test_valuation_collector_commit_existing_schema_and_noop_without_price_reads(collectors):
    c = collectors
    first = run(c)
    assert [(r['strategy'], r['status'], r['rows']) for r in first] == [('估值标尺', 'SUCCESS', 3)]
    assert c.connections == [CONFIG]
    with c.engine.connect() as conn:
        assert conn.scalar(text('SELECT COUNT(*) FROM valuation_ruler_member_v2')) == 3
        import json
        stats = json.loads(conn.scalar(text('SELECT statistics FROM valuation_ruler_daily_v2 LIMIT 1')))
        assert stats['ttm']['eligible'] == 298
        assert stats['ttm']['loss'] == 1
    c.calls.clear()
    second = run(c)
    assert [(r['status'], r['rows']) for r in second] == [('SUCCESS', 0)]
    assert not c.calls
    # V2 lives in separate tables: the legacy importer cannot mix methodologies.


def test_valuation_limit_reports_pending_and_next_run_continues(collectors):
    c = collectors
    c.options['估值标尺']['max_days'] = 1
    result = run(c, ['估值标尺'])[0]
    assert (result['status'], result['rows'], result['remaining']) == ('PENDING', 1, 2)
    c.options['估值标尺']['max_days'] = None
    c.calls.clear()
    result = run(c, ['估值标尺'])[0]
    assert (result['status'], result['rows'], result['skipped']) == ('SUCCESS', 2, 1)
    assert [(call[1], call[2]) for call in c.calls if call[0] == 'pe'] == [(day, size) for day in DAYS[1:] for size in (200, 100)]


def test_failed_valuation_keeps_committed_day_and_batch_continues(collectors):
    c = collectors
    c.failure['pe'] = DAYS[1]
    result = run(c)
    assert [r['status'] for r in result] == ['FAILED']
    with c.engine.connect() as conn:
        assert str(conn.scalar(text('SELECT trade_date FROM valuation_ruler_daily_v2'))) == str(DAYS[0])
    c.failure.clear()
    c.calls.clear()
    assert run(c, ['估值标尺'])[0]['rows'] == 2
    assert [(call[1], call[2]) for call in c.calls if call[0] == 'pe'] == [(day, size) for day in DAYS[1:] for size in (200, 100)]


def test_embedded_history_matches_maintained_sources_and_python36():
    sync = load(ROOT / 'collectors/tools/sync_history_strategies.py', 'sync_index_history_test')
    sync.sync(check=True)


@pytest.mark.parametrize('name', ['估值标尺'])
def test_new_modules_refuse_overwrite_before_connecting(collectors, name):
    c = collectors
    c.options[name]['skip_existing'] = False
    with pytest.raises(ValueError, match='skip_existing'):
        run(c, [name])
    assert c.connections == []
