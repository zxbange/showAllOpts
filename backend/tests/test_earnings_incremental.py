"""Daily disclosure windows and batched quotes preserve transactional collection."""
import ast
import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from test_earnings_collector import c, record, Frame


@pytest.fixture(params=['standalone', 'batch'])
def daily(c, request):
    env = c.env
    state = {'day': dt.date(2026, 10, 16)}
    env.update(china_now=lambda: dt.datetime.combine(state['day'], dt.time(18)),
        get_trade_days=lambda **kw: [state['day']],
        get_price=lambda *a, **kw: pytest.fail('empty selection must not read quotes'),
        get_all_securities=lambda **kw: SimpleNamespace(empty=False, columns=['display_name'],
            iterrows=lambda: iter([('600001.XSHG', {'display_name': '测试'})])),
        MysqlEarnings=SimpleNamespace(connect_to_mysql=lambda: c.engine))
    if request.param == 'batch':
        path = Path(__file__).resolve().parents[2] / 'collectors/策略批量采集_数据库版.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_earnings_strategy')
        node = next(n for n in factory.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
        @contextmanager
        def database_scope(*args):
            yield c.Session
        env.update(settings={'history_mode': 'latest'}, _config={},
            database_scope=database_scope, session_scope=env['_history_session'],
            _require_append=lambda value: None, _progress_note=print, _progress_message=print)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), env)
    return c, state


def test_zero_matches_advance_window_same_day_skips_and_failure_retries(daily):
    c, state = daily
    env = c.env
    calls = []
    def fetch(codes, period, end, start):
        calls.append((start, end))
        return []
    env['fetch_events'] = fetch
    env['screen'] = lambda *a: pytest.fail('no announcements require no quotes')
    assert env['run']() == []
    assert calls == [(state['day'], state['day'])]  # Fresh database: latest close only.
    assert env['run']() == []
    assert len(calls) == 1
    state['day'] = dt.date(2026, 10, 19)  # Monday catches Saturday/Sunday publications too.
    assert env['run']() == []
    assert calls[-1] == (dt.date(2026, 10, 16), state['day'])
    state['day'] = dt.date(2026, 10, 20)
    env['fetch_events'] = lambda *a: (_ for _ in ()).throw(ValueError('source failed'))
    with pytest.raises(ValueError, match='source failed'):
        env['run']()
    with c.Session() as session:
        assert session.query(env['EarningsRun']).one().as_of_date == dt.date(2026, 10, 19)
    env['fetch_events'] = fetch
    assert env['run']() == []
    assert calls[-1] == (dt.date(2026, 10, 19), state['day'])


def test_screen_failure_does_not_advance_window(daily):
    c, state = daily
    env = c.env
    env['fetch_events'] = lambda *args: []
    env['run']()
    previous = state['day']
    state['day'] = dt.date(2026, 10, 19)
    env['fetch_events'] = lambda *args: record(c)['events']
    env['screen'] = lambda *args: (_ for _ in ()).throw(ValueError('quotes missing'))
    with pytest.raises(ValueError, match='quotes missing'):
        env['run']()
    with c.Session() as session:
        assert session.query(env['EarningsRun']).one().as_of_date == previous


def test_write_failure_rolls_back_checked_date(daily):
    from sqlalchemy import event
    c, state = daily
    env = c.env
    windows = []
    def fetch(*args):
        windows.append(args[-1])
        return []
    env['fetch_events'] = fetch
    env['run']()
    previous = state['day']
    state['day'] = dt.date(2026, 10, 19)
    def fail(conn, cursor, statement, *args):
        if statement.startswith('UPDATE earnings_gap_run'):
            raise RuntimeError('write failed')
    event.listen(c.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='write failed'):
            env['run']()
    finally:
        event.remove(c.engine, 'before_cursor_execute', fail)
    with c.Session() as session:
        assert session.query(env['EarningsRun']).one().as_of_date == previous
    env['run']()
    assert windows == [previous, previous, previous]


def test_quarter_switch_uses_previous_success_and_keeps_old_snapshot(c):
    row = record(c, period=dt.date(2026, 6, 30))
    row.update(signal_date=dt.date(2026, 9, 28), announcement_date=dt.date(2026, 9, 28),
               valuation_date=dt.date(2026, 9, 29))
    c.env['save_results'](c.Session, row['report_period'], row['valuation_date'], 1, [row], skip_existing=True)
    assert c.env['scan_start'](c.Session, dt.date(2026, 9, 30), dt.date(2026, 10, 9)) == row['valuation_date']
    with c.Session() as session:
        assert session.query(c.env['EarningsStock']).one().name == row['name']


def test_quotes_use_one_calendar_and_bounded_batches(c):
    env = c.env
    period, pub, end = dt.date(2026, 9, 30), dt.date(2026, 10, 15), dt.date(2026, 12, 15)
    codes = ['600{:03}.XSHG'.format(i) for i in range(121)]
    grouped = {code: [env['event'](code, 'formal', pub, period, 1e8, 30, {})] for code in codes}
    grouped['600999.XSHG'] = [env['event']('600999.XSHG', 'formal', pub, period, 1e8, 5, {})]
    calendar = [d.date() for d in pd.bdate_range(pub-dt.timedelta(days=40), end)]
    calendar_calls, price_calls = [], []
    def trade_days(**kw):
        calendar_calls.append(kw)
        return calendar
    def prices(batch, **kw):
        price_calls.append((list(batch), kw))
        return Frame([dict(code=code, time=day, open=10, close=10.5, high=11, low=9, paused=0, volume=100)
                      for code in batch for day in calendar if kw['start_date'] <= day <= kw['end_date']])
    env.update(get_trade_days=trade_days, get_price=prices)
    rows = list(env['screening_prices'](grouped, end))
    assert len(rows) == 121 and len(calendar_calls) == 1
    assert [len(batch) for batch, _ in price_calls] == [50, 50, 21]
    assert [code for code, _, _ in rows] == codes
    for batch, kwargs in price_calls:
        assert kwargs['panel'] is False and kwargs['fq'] == 'pre'
        assert kwargs['end_date'] == calendar[calendar.index(pub)+20] < end
    assert all(set(bars) == set(d for d in calendar if d <= price_calls[0][1]['end_date']) for _, bars, _ in rows)


def test_empty_express_skips_lazy_mapping_and_reports_source_before_loading(c):
    from sqlalchemy import Column, Date, MetaData, Table
    fields = Table('source_dates', MetaData(), Column('day', Date)).c
    messages = []
    class Jy:
        run_query = None
        @property
        def LC_FSPerformedLetters(self):
            assert messages[-1] == '聚宽：加载快报接口并查询新增披露'
            return SimpleNamespace(EndDate=fields.day, InfoPublDate=fields.day)
        @property
        def SecuMain(self):
            pytest.fail('no express disclosures: must not even load company metadata')
    class Finance:
        run_query = None
        @property
        def STK_FIN_FORCAST(self):
            assert messages[-1] == '聚宽：加载预告接口并查询新增披露'
            return SimpleNamespace(end_date=fields.day, pub_date=fields.day)
    c.env.update(jy=Jy(), finance=Finance(), print=messages.append,
        paged=lambda *a, **kw: [], fetch_formal_events=lambda *a: [])
    assert c.env['fetch_events'](['600001.XSHG'], dt.date(2026,9,30), dt.date(2026,10,16), dt.date(2026,10,15)) == []
    assert len([m for m in messages if '耗时' in m]) == 3


@pytest.mark.parametrize('has_disclosures', [False, True])
def test_formal_disclosures_are_queried_by_dynamic_dates_not_universe_batches(c, has_disclosures):
    from sqlalchemy import Column, Date, Float, MetaData, String, Table
    metadata = MetaData()
    income = Table('formal_income', metadata, Column('code', String), Column('pubDate', Date),
                   Column('statDate', Date), Column('np_parent_company_owners', Float))
    indicator = Table('formal_indicator', metadata, Column('pubDate', Date), Column('statDate', Date),
                      Column('inc_net_profit_to_shareholders_year_on_year', Float))
    start, end, period = dt.date(2026,11,9), dt.date(2026,11,16), dt.date(2026,9,30)
    codes = ['{}.XSHG'.format(600000+i) for i in range(5000)]
    columns = ['code','publication_date','report_period','net_profit','profit_yoy',
               'indicator_publication_date','indicator_period']
    records = [dict(zip(columns, [code, start, period, 100, 25, end, period])) for code in codes[:2]]
    calls = []
    def fetch(q, date):
        sql = str(q.statement)
        params = q.statement.compile().params
        assert ' IN (' not in sql
        assert start in params.values() and end in params.values()
        assert ' OR ' in sql and date == end
        cursor = params['code_1']
        calls.append(cursor)
        # A short server page is not EOF: keep reading until an empty page.
        return Frame([r for r in records if r['code'] > cursor][:1] if has_disclosures else [], columns)
    with c.Session() as session:
        c.env.update(income=income.c, indicator=indicator.c, query=session.query, get_fundamentals=fetch)
        events = c.env['fetch_formal_events'](codes, period, end, start)
    assert calls == (['', codes[0], codes[1]] if has_disclosures else [''])
    assert [e['code'] for e in events] == (codes[:2] if has_disclosures else [])


@pytest.mark.parametrize('fault', ['duplicate', 'wrong_code', 'missing_signal', 'missing_previous'])
def test_invalid_batch_quotes_never_become_partial_results(c, fault):
    env = c.env
    code, pub, period = '600001.XSHG', dt.date(2026, 10, 16), dt.date(2026, 9, 30)
    previous = pub-dt.timedelta(days=1)
    bars = [dict(code=code, time=d, open=10.4, close=10.5 if d==pub else 10, high=11, low=9, paused=0, volume=100)
            for d in (previous, pub)]
    if fault == 'duplicate': bars.append(dict(bars[-1]))
    elif fault == 'wrong_code': bars[-1]['code'] = '600002.XSHG'
    elif fault == 'missing_signal': bars.pop()
    else: bars.pop(0)
    env.update(get_trade_days=lambda **kw: [previous, pub], get_price=lambda *a, **kw: Frame(bars))
    event = env['event'](code, 'formal', pub, period, 1e8, 30, {})
    with pytest.raises(ValueError, match='行情'):
        env['screen']([code], {code: '测试'}, [event], period, pub)
