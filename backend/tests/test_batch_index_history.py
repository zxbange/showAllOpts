"""Run the two embedded collectors against real SQLite tables and synthetic platform APIs."""
import datetime as dt
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import func, select

from test_collector_batch import CONFIG, load
from app.services import growth_dividend_import as growth
from app.services import valuation_ruler_import as valuation

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
        calls.append(('pe', date))
        if failure.get('pe') == date:
            raise RuntimeError('synthetic PE provider failure')
        return pd.DataFrame({'code': codes, 'day': [date]*300,
                             'pe_ratio': [None, -2] + [10]*298})
    def prices(code, start_date, end_date, **kwargs):
        assert kwargs['fq'] is None and kwargs['fields'] == ['close']
        calls.append(('price', code, start_date, end_date))
        dates = [day for day in DAYS if start_date <= day <= end_date and failure.get(code) != day]
        return pd.DataFrame({'close': [4000]*len(dates)}, index=dates)
    apis = dict(get_all_trade_days=lambda: DAYS,
                get_trade_days=lambda end_date, start_date=DAYS[0]: [d for d in DAYS if start_date <= d <= end_date],
                get_index_stocks=lambda index, date: codes,
                valuation=SimpleNamespace(code=SimpleNamespace(in_=lambda codes: codes), day=object(), pe_ratio=object()),
                query=lambda *args: SimpleNamespace(filter=lambda _: 'dated PE'),
                get_fundamentals=fundamentals, get_price=prices)
    options = {name: dict(start_date=str(DAYS[0]), end_date=str(DAYS[-1])) for name in ('估值标尺', '创红比')}
    return SimpleNamespace(batch=batch, apis=apis, options=options, calls=calls, failure=failure,
                           connections=connections, engine=engine)


def run(c, names=None):
    return c.batch.run_all(names or ['估值标尺', '创红比'], options=c.options, db_config=CONFIG,
                           api_namespace=c.apis, preview_rows=0, raise_on_failure=False)


def test_both_collectors_commit_existing_schema_and_noop_without_price_reads(collectors):
    c = collectors
    first = run(c)
    assert [(r['strategy'], r['status'], r['rows']) for r in first] == [('估值标尺', 'SUCCESS', 3), ('创红比', 'SUCCESS', 9)]
    assert c.connections == [CONFIG, CONFIG]
    with c.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(valuation.MEMBER)) == 900
        assert conn.scalar(select(func.count()).select_from(growth.PRICES)) == 9
        assert conn.scalar(select(valuation.DAILY.c.positive_count).limit(1)) == 298
        assert conn.scalar(select(valuation.DAILY.c.loss_count).limit(1)) == 1
    c.calls.clear()
    second = run(c)
    assert [(r['status'], r['rows']) for r in second] == [('SUCCESS', 0), ('SUCCESS', 0)]
    assert not c.calls
    # The backend's actual read-only importers accept the generated records.
    with valuation.read_source(c.engine, DAYS[-1], lambda: None) as data:
        assert len(list(data.points())) == 3
    days, points = growth.read_source(c.engine, DAYS[-1], lambda: None)
    assert days == DAYS and len(points) == 9


def test_valuation_limit_reports_pending_and_next_run_continues(collectors):
    c = collectors
    c.options['估值标尺']['max_days'] = 1
    result = run(c, ['估值标尺'])[0]
    assert (result['status'], result['rows'], result['remaining']) == ('PENDING', 1, 2)
    c.options['估值标尺']['max_days'] = None
    c.calls.clear()
    result = run(c, ['估值标尺'])[0]
    assert (result['status'], result['rows'], result['skipped']) == ('SUCCESS', 2, 1)
    assert [call[1] for call in c.calls if call[0] == 'pe'] == DAYS[1:]


def test_failed_valuation_keeps_committed_day_and_batch_continues(collectors):
    c = collectors
    c.failure['pe'] = DAYS[1]
    result = run(c)
    assert [r['status'] for r in result] == ['FAILED', 'SUCCESS']
    with c.engine.connect() as conn:
        assert conn.execute(select(valuation.DAILY.c.trade_date)).scalars().all() == DAYS[:1]
    c.failure.clear()
    c.calls.clear()
    assert run(c, ['估值标尺'])[0]['rows'] == 2
    assert [call[1] for call in c.calls if call[0] == 'pe'] == DAYS[1:]


def test_growth_mid_history_gap_is_partial_then_filled_without_rereading_good_dates(collectors):
    c = collectors
    c.failure['399006.XSHE'] = DAYS[1]
    result = run(c, ['创红比'])[0]
    assert (result['status'], result['rows'], result['gaps']) == ('PARTIAL', 8, 1)
    c.failure.clear()
    c.calls.clear()
    result = run(c, ['创红比'])[0]
    assert (result['status'], result['rows'], result['gaps']) == ('SUCCESS', 1, 0)
    assert c.calls == [('price', '399006.XSHE', DAYS[1], DAYS[1])]


def test_growth_unknown_prefix_is_visible_but_not_queried_each_daily_run(collectors):
    c = collectors
    c.failure['399006.XSHE'] = DAYS[0]
    first = run(c, ['创红比'])[0]
    assert first['rows'] == 8
    assert first['coverage']['399006.XSHE']['observed_first_day'] == str(DAYS[1])
    assert first['coverage']['399006.XSHE']['missing'] == 1
    c.calls.clear()
    assert run(c, ['创红比'])[0]['rows'] == 0
    assert not c.calls
    c.failure.clear()
    c.options['创红比']['retry_earlier_missing'] = True
    assert run(c, ['创红比'])[0]['rows'] == 1
    assert c.calls == [('price', '399006.XSHE', DAYS[0], DAYS[0])]


def test_empty_index_is_partial_and_retried_on_next_run(collectors):
    c = collectors
    original = c.apis['get_price']
    def price(code, **kwargs):
        return pd.DataFrame(columns=['close']) if code == '399006.XSHE' else original(code, **kwargs)
    c.apis['get_price'] = price
    first = run(c, ['创红比'])[0]
    assert (first['status'], first['rows'], first['gaps']) == ('PARTIAL', 6, 3)
    assert first['coverage']['399006.XSHE']['observed_first_day'] is None
    c.apis['get_price'] = original
    c.calls.clear()
    second = run(c, ['创红比'])[0]
    assert (second['status'], second['rows'], second['gaps']) == ('SUCCESS', 3, 0)
    assert c.calls == [('price', '399006.XSHE', DAYS[0], DAYS[-1])]


def test_embedded_history_matches_maintained_sources_and_python36():
    sync = load(ROOT / 'collectors/tools/sync_history_strategies.py', 'sync_index_history_test')
    sync.sync(check=True)


@pytest.mark.parametrize('name', ['估值标尺', '创红比'])
def test_new_modules_refuse_overwrite_before_connecting(collectors, name):
    c = collectors
    c.options[name]['skip_existing'] = False
    with pytest.raises(ValueError, match='skip_existing'):
        run(c, [name])
    assert c.connections == []
