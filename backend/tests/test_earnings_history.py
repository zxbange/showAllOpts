"""Historical replay is append-only, dated, bounded, and checkpointed separately."""
import datetime as dt
from types import SimpleNamespace
import pandas as pd
import pytest
from sqlalchemy import Column, Date, MetaData, String, Table
from test_earnings_collector import c, record, Frame


def setup_history(c):
    env = c.env
    period = dt.date(2026, 9, 30)
    day = dt.date(2026, 10, 16)
    dates = [dt.date(2026, 9, 29), period, dt.date(2026, 10, 15), day]
    table = Table('income', MetaData(), Column('code', String), Column('statDate', Date))
    env.update(china_now=lambda: dt.datetime(2026, 10, 17, 17),
        get_all_trade_days=lambda: dates,
        income=SimpleNamespace(code=table.c.code, statDate=table.c.statDate),
        jy=SimpleNamespace(LC_FSPerformedLetters=SimpleNamespace(EndDate='end'), run_query=None),
        finance=SimpleNamespace(STK_FIN_FORCAST=SimpleNamespace(end_date='end'), run_query=None),
        query=lambda *a: a,
        _history_source_floor=lambda *a: period,
        get_fundamentals=lambda *a, **k: Frame([{'code': '600001.XSHG', 'statDate': period}]),
        get_all_securities=lambda **kw: pd.DataFrame({
            'display_name': ['测试'], 'start_date': [dt.date(2000, 1, 1)],
            'end_date': [dt.date(2200, 1, 1)]}, index=['600001.XSHG']),
        fetch_events=lambda *a: [], fetch_formal_events=lambda *a: [],
        screen=lambda *a, **k: [record(c)],
        _history_events=lambda p, d, cache: ({'600001.XSHG': '测试'}, [env['event']('600001.XSHG', 'formal', '2026-10-15', p, 1e8, 25, {})], True))
    return period, day, dates


def test_quarter_sequence_and_invalid_dates(c):
    periods = list(c.env['_history_periods']('2024-12-31', '2025-12-31'))
    assert [str(p) for p in periods] == ['2024-12-31', '2025-03-31', '2025-06-30', '2025-09-30', '2025-12-31']
    with pytest.raises(ValueError, match='季末'):
        list(c.env['_history_periods']('2024-12-30', '2025-12-31'))


def test_replay_checkpoint_skips_and_preserves_existing_snapshot(c):
    period, day, dates = setup_history(c)
    run = c.env['collect_history']
    assert len(run(c.Session, day, 'gaps', period, 1)) == 1
    c.env['screen'] = lambda *a, **kw: pytest.fail('completed quarter should skip')
    assert run(c.Session, day, 'gaps', period, 1) == []
    c.env['screen'] = lambda *a, **kw: [dict(record(c), name='revised name')]
    assert run(c.Session, day, 'full', period, 1) == []
    with c.Session() as s:
        assert s.query(c.env['EarningsStock']).one().name == '测试公司'
        assert s.execute(c.env['_history_checkpoint_table']().select()).one().as_of_date == day


def test_no_finance_coverage_never_creates_empty_quarter_or_checkpoint(c):
    period, day, _ = setup_history(c)
    c.env['_history_events'] = lambda *a: ({}, [], False)
    assert c.env['collect_history'](c.Session, day, 'gaps', period, 1) == []
    with c.Session() as s:
        assert s.query(c.env['EarningsRun']).count() == 0
        assert s.execute(c.env['_history_checkpoint_table']().select()).one().method == 'unknown-empty-v2'
        assert s.execute(c.env['_earnings_version_table']().select()).first() is None
    c.env['_history_events'] = lambda *a: pytest.fail('same cutoff should resume past unknown')
    assert c.env['collect_history'](c.Session, day, 'gaps', period, 1) == []


def test_failed_quarter_has_no_checkpoint_and_successful_snapshot_survives(c):
    period, day, _ = setup_history(c)
    c.env['_migrate_earnings_versions'] = lambda *a: (_ for _ in ()).throw(ValueError('migration failed'))
    with pytest.raises(ValueError, match='migration failed'):
        c.env['collect_history'](c.Session, day, 'gaps', period, 1)
    with c.Session() as s:
        assert s.query(c.env['EarningsStock']).count() == 1
        assert s.execute(c.env['_history_checkpoint_table']().select()).first() is None


def test_history_provenance_is_explicit_and_preserves_publication(c):
    period, day, _ = setup_history(c)
    captured = []
    def screen(codes, names, events, p, cutoff, historical=False):
        captured.extend(events)
        assert historical and cutoff == day
        return []
    c.env['screen'] = screen
    c.env['collect_history'](c.Session, day, 'gaps', period, 1)
    assert captured[0]['source_kind'] == 'joinquant_current_visible_history'
    assert captured[0]['historical_cutoff'] == str(day)
    assert captured[0]['publication_date'] == '2026-10-15'


def test_source_floor_empty_or_bad_schema_is_not_verified_coverage(c):
    table = Table('source', MetaData(), Column('end_date', Date))
    from sqlalchemy import select
    c.env['query'] = lambda *fields: select(*fields)
    for frame in (Frame([], ['end_date']), None, Frame([{'wrong': 1}])):
        with pytest.raises(ValueError):
            c.env['_history_source_floor'](table, table.c.end_date, lambda q: frame)


@pytest.mark.parametrize('mode,limit', [('bad', 1), ('gaps', 0), ('gaps', True), ('gaps', 1.5)])
def test_invalid_history_controls_fail_before_queries(c, mode, limit):
    period, day, _ = setup_history(c)
    with pytest.raises(ValueError):
        c.env['collect_history'](c.Session, day, mode, period, limit)


def test_unknown_attempts_are_bounded_and_next_run_advances(c):
    period, day, _ = setup_history(c)
    seen = []
    c.env['get_all_trade_days'] = lambda: [dt.date(2026, 1, 2), day]
    c.env['_history_events'] = lambda p, *args: (seen.append(p) or ({}, [], False))
    c.env['collect_history'](c.Session, day, 'gaps', None, 1)
    c.env['collect_history'](c.Session, day, 'gaps', None, 1)
    assert seen == [dt.date(2026, 3, 31), dt.date(2026, 6, 30)]
    with c.Session() as s:
        assert s.query(c.env['EarningsRun']).count() == 0


def test_dated_announcement_pool_includes_delisted_issuer_without_daily_replay(c):
    helper = c.env['_history_events']
    period, day, _ = setup_history(c)
    from sqlalchemy import Integer, select
    meta = MetaData()
    express = Table('express', meta, Column('EndDate', Date), Column('InfoPublDate', Date))
    forecast = Table('forecast', meta, Column('end_date', Date), Column('pub_date', Date))
    mapping = Table('mapping', meta, Column('SecuCategory', Integer))
    c.env.update(jy=SimpleNamespace(LC_FSPerformedLetters=express.c, SecuMain=mapping.c, run_query=None),
                 finance=SimpleNamespace(STK_FIN_FORCAST=forecast.c, run_query=None))
    c.env['paged'] = lambda table, *a: ([{'SecuCode': '600001', 'CompanyCode': 1}] if table is mapping.c else [])
    c.env['_history_formal_events'] = lambda p, d: [c.env['event']('600001.XSHG', 'formal', period, p, 1, 25, {})]
    dates = []
    def pool(**kw):
        dates.append(kw['date'])
        assert kw['date'] == period
        return pd.DataFrame({'display_name': ['已退市历史股'], 'start_date': [dt.date(2000, 1, 1)],
            'end_date': [period]}, index=['600001.XSHG'])
    c.env['get_all_securities'] = pool
    names, events, verified = helper(period, day, {})
    assert verified and names == {'600001.XSHG': '已退市历史股'}
    assert len(events) == 1 and dates == [period]


def test_quarter_formal_keyset_pagination_checks_cutoffs_and_progress(c):
    from sqlalchemy import select, Float
    meta = MetaData()
    income = Table('inc', meta, Column('code', String), Column('pubDate', Date),
        Column('statDate', Date), Column('np_parent_company_owners', Float))
    ind = Table('ind', meta, Column('pubDate', Date), Column('statDate', Date),
        Column('inc_net_profit_to_shareholders_year_on_year', Float))
    c.env.update(income=income.c, indicator=ind.c, query=lambda *fields: select(*fields))
    p, day = dt.date(2026, 9, 30), dt.date(2026, 10, 16)
    row = dict(code='600001.XSHG', publication_date=dt.date(2026, 10, 15), report_period=p,
        net_profit=1e8, profit_yoy=25, indicator_publication_date=day, indicator_period=p)
    calls = []
    def fetch(q, **kw):
        calls.append((q, kw))
        return Frame([row] if len(calls) == 1 else [], list(row))
    c.env['get_fundamentals'] = fetch
    events = c.env['_history_formal_events'](p, day)
    assert events[0]['publication_date'] == str(day)
    assert len(calls) == 2 and all(kw == {'statDate': '2026q3'} for _, kw in calls)
    sql = str(calls[0][0])
    assert 'inc."pubDate" <=' in sql and 'ind."pubDate" <=' in sql and 'ORDER BY inc.code' in sql
    # Even if the server ignores the filter, a future announcement is rejected.
    c.env['get_fundamentals'] = lambda *a, **kw: Frame([dict(row, publication_date=day+dt.timedelta(days=1))])
    with pytest.raises(ValueError, match='截止日期后'):
        c.env['_history_formal_events'](p, day)
    c.env['get_fundamentals'] = lambda *a, **kw: Frame([row])
    with pytest.raises(ValueError, match='未前进'):
        c.env['_history_formal_events'](p, day)
