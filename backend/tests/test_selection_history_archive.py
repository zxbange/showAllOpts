"""Offline archive contracts; historical recomputation is explicitly not original PIT."""
import datetime as dt
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Date, Integer, Numeric, create_engine, inspect, event
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]
DAYS = [dt.date(2024, 1, n) for n in (2, 3, 4)]


@pytest.fixture(params=['高股息选股', '高增长'])
def scenario(request, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    spec = importlib.util.spec_from_file_location('archive_test', ROOT/'collectors'/(request.param+'_数据库版.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'PROGRESS_ENABLED', False)
    monkeypatch.setattr(mod, 'china_now', lambda: dt.datetime(2026, 11, 11, 18))
    engine = create_engine('sqlite:///' + str(tmp_path / 'archive.db'))
    monkeypatch.setattr(mod, 'connect_database', lambda config: engine)
    fn = mod._dividend_strategy if request.param == '高股息选股' else mod._growth_strategy
    ns = fn({'get_trade_days': lambda **kw: DAYS, 'get_all_trade_days': lambda: DAYS}, mod.DB_CONFIG)
    snapshot = getattr(ns, 'DividendSnapshot', getattr(ns, 'GrowthSnapshot', None))
    stock = getattr(ns, 'DividendStock', getattr(ns, 'GrowthStock', None))
    field = 'trade_date' if request.param == '高股息选股' else 'as_of_date'
    calls, fail = [], set()
    def calc(day, *args, **kwargs):
        calls.append(day)
        if day in fail: raise RuntimeError('password=DO_NOT_STORE mysql://user:secret@host/db')
        meta = {c.name: day if isinstance(c.type, Date) else 1 for c in snapshot.__table__.columns
                if c.name not in ('id', 'updated_at')}
        meta['rule_version'] = ns.RULE_VERSION
        if 'value_count' in meta: meta['value_count'] = 0
        record = {}
        for c in stock.__table__.columns:
            if isinstance(c.type, Date): record[c.name] = day
            elif isinstance(c.type, Numeric): record[c.name] = Decimal('1234567890123456.123456')
            elif isinstance(c.type, Integer): record[c.name] = 1
            else: record[c.name] = None if c.nullable else 'sample'
        record.update(code='000001.XSHE', name='样本', rank=1)
        if 'method' in record: record['method'] = 'classic'
        if 'reports' in record: record['reports'] = {'annual': [{'net_profit': '12345678901234567890.12345678'}]}
        return meta, [record]
    cells = dict(zip(ns.archive_history.__code__.co_freevars, ns.archive_history.__closure__))
    cells['calculate'].cell_contents = calc
    yield mod, ns, engine, calls, fail, calc, snapshot, stock, field
    engine.dispose()


def test_bounded_resume_precision_no_latest(scenario):
    mod, ns, engine, calls, fail, calc, snapshot, stock, field = scenario
    first = ns.archive_history(DAYS[0], DAYS[-1], max_days=1)
    assert first['added'] == 1 and first['remaining'] == 2 and not first['original_point_in_time']
    assert inspect(engine).get_table_names() == [ns.SelectionArchive.__tablename__]
    second = ns.run(mode='history', start_date=DAYS[0], end_date=DAYS[-1])
    assert second['added'] == 2 and second['skipped'] == 1 and second['remaining'] == 0
    assert calls == DAYS
    assert ns.archive_history(DAYS[0], DAYS[-1])['added'] == 0 and calls == DAYS
    with sessionmaker(bind=engine)() as session:
        row = session.query(ns.SelectionArchive).first()
        assert row.status == 'complete' and '1234567890123456.123456' in row.stocks_json
        assert json.loads(row.snapshot_json)[field] == str(DAYS[0])
        assert json.loads(row.provenance_json)['original_point_in_time'] is False
        assert row.captured_at and row.rule_version == ns.RULE_VERSION
    with pytest.raises(ValueError, match='实际采集'): ns.restore_observed(DAYS[0])
    assert snapshot.__tablename__ not in inspect(engine).get_table_names()


def test_safe_gap_and_retry(scenario):
    mod, ns, engine, calls, fail, *unused = scenario
    fail.add(DAYS[1])
    with pytest.raises(RuntimeError, match='保留缺口') as error:
        ns.archive_history(DAYS[0], DAYS[-1])
    assert 'secret' not in str(error.value)
    with sessionmaker(bind=engine)() as session:
        rows = session.query(ns.SelectionArchive).order_by(ns.SelectionArchive.as_of_date).all()
        assert [r.status for r in rows] == ['complete', 'gap']
        assert rows[1].error_code == 'RuntimeError' and rows[1].stocks_json is None
        assert 'secret' not in rows[1].provenance_json
    fail.clear()
    result = ns.archive_history(DAYS[0], DAYS[-1], retry_gaps=True)
    assert result['added'] == 2 and result['skipped'] == 1 and calls.count(DAYS[0]) == 1


def test_partial_reports_gaps(scenario):
    _, ns, _, _, fail, *unused = scenario
    fail.add(DAYS[1])
    result = ns.archive_history(DAYS[0], DAYS[-1], allow_partial=True)
    assert result['added'] == 2 and result['gaps'] == [{'date': str(DAYS[1]), 'error_code': 'RuntimeError'}]


def test_preview_and_strict_pit_do_not_connect(scenario, monkeypatch):
    mod, ns, engine, calls, *unused = scenario
    monkeypatch.setattr(mod, 'connect_database', lambda _: pytest.fail('must not connect'))
    assert ns.archive_history(DAYS[0], DAYS[-1], dry_run=True)['remaining'] == 3
    with pytest.raises(ValueError, match='不能可靠还原'):
        ns.archive_history(DAYS[0], DAYS[-1], source_kind='point_in_time')
    assert calls == [] and inspect(engine).get_table_names() == []


@pytest.mark.parametrize('value', [0, -1, True, 251, 1.5])
def test_invalid_bounds(scenario, value):
    _, ns, engine, *unused = scenario
    with pytest.raises(ValueError, match='max_days'): ns.archive_history(DAYS[0], DAYS[-1], max_days=value)
    assert inspect(engine).get_table_names() == []


def test_observed_immutable_restore_and_no_rewind(scenario):
    mod, ns, engine, calls, fail, calc, snapshot, stock, field = scenario
    Session = sessionmaker(bind=engine); ns.ArchiveBase.metadata.create_all(engine)
    meta, records = calc(DAYS[0])
    assert ns.save_archive(Session, DAYS[0], 'observed', meta, records)
    assert not ns.save_archive(Session, DAYS[0], 'observed', dict(meta, universe_count=999), records)
    assert ns.restore_observed(DAYS[0])
    with Session() as session:
        assert session.query(snapshot).one().universe_count == 1
        assert session.query(stock).one().code == records[0]['code']
    newer_meta, newer_records = calc(DAYS[2]); ns.save_latest(Session, newer_meta, newer_records)
    with pytest.raises(ValueError, match='更新日期'): ns.restore_observed(DAYS[0])


def test_transaction_rollback_and_longtext(scenario):
    _, ns, engine, calls, fail, calc, *unused = scenario
    ns.ArchiveBase.metadata.create_all(engine); Session = sessionmaker(bind=engine)
    meta, records = calc(DAYS[0])
    assert str(ns.SelectionArchive.__table__.c.stocks_json.type.compile(dialect=mysql.dialect())) == 'LONGTEXT'
    def stop(mapper, connection, target): raise RuntimeError('write failed')
    event.listen(ns.SelectionArchive, 'before_insert', stop)
    with pytest.raises(RuntimeError, match='write failed'):
        ns.save_archive(Session, DAYS[0], 'revised_asof', meta, records)
    event.remove(ns.SelectionArchive, 'before_insert', stop)
    with Session() as session: assert session.query(ns.SelectionArchive).count() == 0
    assert ns.save_archive(Session, DAYS[0], 'revised_asof', meta, records)


def test_earliest_default_and_calendar_hole_rejected(scenario):
    _, ns, engine, calls, *unused = scenario
    assert ns.archive_history(end_date=DAYS[-1], dry_run=True)['dates'] == [str(d) for d in DAYS]
    cells = dict(zip(ns.archive_history.__code__.co_freevars, ns.archive_history.__closure__))
    cells['get_trade_days'].cell_contents = lambda **kw: [DAYS[0], DAYS[-1]]
    with pytest.raises(ValueError, match='完整交易日历不一致'):
        ns.archive_history(end_date=DAYS[-1])
    assert calls == [] and inspect(engine).get_table_names() == []


def test_gaps_do_not_starve_later_history_and_require_explicit_retry(scenario):
    _, ns, _, calls, fail, *unused = scenario
    fail.add(DAYS[0])
    first = ns.archive_history(end_date=DAYS[-1], max_days=1, allow_partial=True)
    assert first['remaining'] == 2 and first['gaps']
    second = ns.archive_history(end_date=DAYS[-1], max_days=1, allow_partial=True)
    assert second['added'] == 1 and second['previous_gaps'][0]['date'] == str(DAYS[0])
    assert calls == DAYS[:2]
    fail.clear()
    third = ns.archive_history(end_date=DAYS[-1], retry_gaps=True)
    assert third['added'] == 2 and calls.count(DAYS[0]) == 2


def test_incompatible_existing_schema_preserved(scenario):
    from sqlalchemy import text
    _, ns, engine, calls, *unused = scenario
    name = ns.SelectionArchive.__tablename__
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE '+name+' (as_of_date DATE PRIMARY KEY, sentinel TEXT)'))
        connection.execute(text('INSERT INTO '+name+" VALUES ('2024-01-02', 'keep')"))
    with pytest.raises(ValueError, match='字段或主键不兼容'):
        ns.archive_history(end_date=DAYS[-1])
    with engine.connect() as connection:
        assert connection.execute(text('SELECT sentinel FROM '+name)).scalar() == 'keep'
    assert calls == []


def test_empty_valid_selection_is_completed_only_with_valid_metadata(scenario):
    _, ns, engine, calls, fail, calc, snapshot, stock, field = scenario
    ns.ArchiveBase.metadata.create_all(engine)
    meta, _ = calc(DAYS[0])
    for key in ('classic_count', 'value_count', 'matched_count'):
        if key in meta: meta[key] = 0
    assert ns.save_archive(sessionmaker(bind=engine), DAYS[0], 'revised_asof', meta, [])
    with sessionmaker(bind=engine)() as session:
        saved = session.query(ns.SelectionArchive).one()
        assert saved.status == 'complete' and saved.stocks_json == '[]'


def test_growth_short_pages_are_not_exhaustion(scenario):
    from types import SimpleNamespace
    from sqlalchemy import Table, MetaData, Column, String
    import pandas as pd
    mod, ns, engine, *unused = scenario
    if not hasattr(mod, '_growth_strategy'):
        return
    source = create_engine('sqlite://')
    table = Table('reports', MetaData(), Column('id', Integer, primary_key=True), Column('code', String),
        Column('pub_date', Date), Column('start_date', Date), Column('end_date', Date),
        Column('report_date', Date), Column('report_type', Integer),
        Column('np_parent_company_owners', Numeric), Column('basic_eps', Numeric))
    table.metadata.create_all(source)
    with source.begin() as connection:
        connection.execute(table.insert(), [dict(id=i+1, code='a', pub_date=dt.date(2025, 1, 10),
            start_date=dt.date(year, 1, 1), end_date=dt.date(year, month, day),
            report_date=dt.date(year, month, day), report_type=0,
            np_parent_company_owners=100, basic_eps=1)
            for i, (year, month, day) in enumerate((y,m,d) for y in (2022,2023,2024)
                for m,d in ((3,31),(6,30),(9,30),(12,31)))])
    with sessionmaker(bind=source)() as session:
        def fetch(query):
            rows = [dict(row._mapping) for row in query.limit(1).all()]
            return pd.DataFrame(rows, columns=table.columns.keys())
        worker = mod._growth_strategy({'query': session.query,
            'finance': SimpleNamespace(STK_INCOME_STATEMENT=table.c, run_query=fetch)}, mod.DB_CONFIG)
        result = worker.financial_reports(['a'], dt.date(2025, 1, 11))
        assert len(result['a']) == 12
    source.dispose()


def test_dividend_history_rejects_partial_financial_coverage(scenario):
    from sqlalchemy import Table, MetaData, Column, String
    import pandas as pd
    from types import SimpleNamespace
    mod, ns, engine, *unused = scenario
    if not hasattr(mod, '_dividend_strategy'):
        return
    fields = ('inc_total_revenue_year_on_year', 'inc_net_profit_year_on_year', 'inc_return', 'statDate', 'pubDate')
    table = Table('indicators', MetaData(), Column('code', String),
                  *[Column(field, Date if field in ('statDate','pubDate') else Numeric) for field in fields])
    query = lambda *args: SimpleNamespace(filter=lambda *args: None)
    frame = pd.DataFrame([dict(code='a', inc_total_revenue_year_on_year=10, inc_net_profit_year_on_year=20,
                               inc_return=6, statDate='2023-09-30', pubDate='2023-10-20')])
    worker = mod._dividend_strategy({'query': query, 'indicator': table.c,
        'get_fundamentals': lambda *args, **kwargs: frame}, mod.DB_CONFIG)
    assert set(worker.financials(['a', 'b'], DAYS[0])) == {'a'}
    with pytest.raises(ValueError, match='未完整返回'):
        worker.financials(['a', 'b'], DAYS[0], strict_history=True)
    frame.loc[0, 'pubDate'] = None
    with pytest.raises(ValueError, match='缺少公告日期'):
        worker.financials(['a'], DAYS[0], strict_history=True)


def test_explicit_none_processes_fixed_calendar_over_250_days(scenario):
    _, ns, engine, calls, fail, *unused = scenario
    days = [dt.date(2023, 1, 1) + dt.timedelta(days=n) for n in range(260)]
    cells = dict(zip(ns.archive_history.__code__.co_freevars, ns.archive_history.__closure__))
    cells['_apis'].cell_contents['get_all_trade_days'] = lambda: list(days)
    calendar_calls = []
    def calendar(**kwargs):
        calendar_calls.append(kwargs)
        return list(days)
    cells['get_trade_days'].cell_contents = calendar
    preview = ns.archive_history(end_date=days[-1], max_days=None, dry_run=True)
    assert preview['remaining'] == 260 and calls == []
    assert inspect(engine).get_table_names() == []
    fail.add(days[1])
    result = ns.archive_history(end_date=days[-1], max_days=None, allow_partial=True)
    assert result['added'] == 259 and result['remaining'] == 0
    assert result['gaps'] == [{'date': str(days[1]), 'error_code': 'RuntimeError'}]
    assert calls == days and len(calendar_calls) == 2
    # A rerun skips both complete dates and prior gaps, with no future polling.
    assert ns.archive_history(end_date=days[-1], max_days=None)['added'] == 0
    assert calls == days and len(calendar_calls) == 3
    fail.clear()
    assert ns.archive_history(end_date=days[-1], max_days=None, retry_gaps=True)['added'] == 1
    assert calls == days + [days[1]]


def test_final_unresolved_gap_count_after_skips_and_bounded_retry(scenario):
    _, ns, _, _, fail, *unused = scenario
    assert ns.archive_history(end_date=DAYS[-1], dry_run=True)['unresolved_gaps'] is None
    fail.update((DAYS[0], DAYS[2]))
    assert ns.archive_history(end_date=DAYS[-1], allow_partial=True)['unresolved_gaps'] == 2
    skipped = ns.archive_history(end_date=DAYS[-1])
    assert skipped['remaining'] == 0 and skipped['gaps'] == [] and skipped['unresolved_gaps'] == 2
    fail.clear()
    bounded = ns.archive_history(end_date=DAYS[-1], max_days=1, retry_gaps=True)
    assert bounded['added'] == 1 and bounded['remaining'] == 1 and bounded['unresolved_gaps'] == 1
    final = ns.archive_history(end_date=DAYS[-1], retry_gaps=True)
    assert final['remaining'] == 0 and final['unresolved_gaps'] == 0
