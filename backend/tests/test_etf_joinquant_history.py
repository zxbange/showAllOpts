"""Offline single-file JoinQuant history backfill regression; never uses a real account/DB."""
import datetime as dt
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import numpy as np
import pytest
from sqlalchemy import BigInteger, Column, Date, String, create_engine, event
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker

from app.core.etf_catalog import ETF_GROUPS
from app.models.etf import ETFPrice, ETFShare, ETFTradingDay
from app.repositories.etf import ETFRepository
from app.services.etf import ETFService

SCRIPT = Path(__file__).resolve().parents[2] / 'collectors' / 'ETF历史补采_聚宽研究版.py'
CODE = '510300.XSHG'
DATES = [dt.date(2023, 12, 28), dt.date(2023, 12, 29),
         dt.date(2024, 1, 2), dt.date(2024, 1, 3), dt.date(2024, 1, 4), dt.date(2024, 1, 5)]


@pytest.fixture
def backfill(tmp_path):
    spec = importlib.util.spec_from_file_location('joinquant_history_offline', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.china_now = lambda: dt.datetime(2024, 1, 7, 8)  # Sunday; yesterday is Saturday.
    engine = create_engine('sqlite:///' + str(tmp_path / 'history.sqlite'))
    module.Base.metadata.create_all(engine)
    class Source(module.Base):
        __tablename__ = 'offline_source'
        code = Column(String, primary_key=True)
        date = Column(Date, primary_key=True)
        shares = Column(BigInteger)
    source_session = sessionmaker(bind=engine)()
    share_values = {(CODE, day): 100 for day in DATES[1:]}
    price_values = {(CODE, day): Decimal('2.000') for day in DATES[1:]}
    info = {CODE: SimpleNamespace(code=CODE, start_date=dt.date(2012, 1, 1), end_date=dt.date(2200, 1, 1))}
    requests = []
    def get_trade_days(start_date=None, end_date=None, count=None):
        days = [day for day in DATES if (start_date is None or day >= start_date)
                and (end_date is None or day <= end_date)]
        return days[-count:] if count else days
    def run_query(query):
        params = query.statement.compile().params
        code, start, end = params['code_1'], params['date_1'], params['date_2']
        requests.append(('shares', code, start, end))
        assert 'LIMIT' in str(query.statement)
        return pd.DataFrame([dict(code=code, date=day, shares=value)
                             for (fund, day), value in share_values.items()
                             if fund == code and start <= day <= end], columns=['code', 'date', 'shares'])
    def get_price(code, **kwargs):
        assert kwargs['fq'] is None and kwargs['skip_paused'] is True
        assert kwargs['frequency'] == 'daily' and kwargs['fields'] == ['close', 'paused']
        start, end = kwargs['start_date'], kwargs['end_date']
        requests.append(('prices', code, start, end))
        data = [(day, value) for (fund, day), value in price_values.items()
                if fund == code and start <= day <= end]
        return pd.DataFrame([dict(close=value, paused=0) for day, value in data],
                            index=pd.DatetimeIndex([day for day, value in data]), columns=['close', 'paused'])
    jq = SimpleNamespace(query=source_session.query,
                         finance=SimpleNamespace(FUND_SHARE_DAILY=Source, run_query=run_query),
                         get_trade_days=get_trade_days, get_price=get_price,
                         get_security_info=lambda code: info.get(code))
    module.load_api = lambda: jq
    module.ETF_GROUPS = {'hs300': [(CODE, 'test ETF')]}
    connections = []
    def connect(config):
        connections.append(True)
        return engine
    module.connect_database = connect
    yield SimpleNamespace(module=module, engine=engine, jq=jq, shares=share_values, prices=price_values,
                          info=info, requests=requests, connections=connections)
    source_session.close()
    engine.dispose()


def get_rows(c, model):
    with sessionmaker(bind=c.engine)() as session:
        return session.query(model).order_by(model.trade_date).all()


def test_defaults_catalog_and_exact_three_table_schema(backfill):
    # Read an independent copy to compare the unmodified catalogue.
    spec = importlib.util.spec_from_file_location('original_history', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.START_DATE == '2024-01-01' and module.END_DATE is None
    assert module.ETF_GROUPS == ETF_GROUPS
    assert sum(map(len, module.ETF_GROUPS.values())) == 18
    for actual, expected in zip(module.MODELS, (ETFShare, ETFPrice, ETFTradingDay)):
        assert actual.__tablename__ == expected.__tablename__
        assert [(x.name, str(x.type), x.primary_key, x.nullable) for x in actual.__table__.columns] == [
            (x.name, str(x.type), x.primary_key, x.nullable) for x in expected.__table__.columns]
        assert 'CREATE TABLE' not in str(actual.__table__.insert().compile(dialect=mysql.dialect()))


def test_default_preview_and_weekend_end_no_database_or_2023_records(backfill):
    c = backfill
    result = c.module.run()
    assert not c.connections and result['written'] is False
    assert result['source_complete'] is True
    assert result['start_date'] == dt.date(2024, 1, 1)
    assert result['end_date'] == DATES[-1]
    assert result['reference_date'] == DATES[1]
    assert all(row['trade_date'].year == 2024 for key in ('shares', 'prices', 'trading_days') for row in result[key])
    assert result['shares'][0]['previous_trade_date'] == DATES[1]
    assert len(result['shares']) == len(result['prices']) == len(result['trading_days']) == 4
    assert get_rows(c, c.module.ETFShare) == []


@pytest.mark.parametrize('pollution', ['any', 'numpy_namespace'])
@pytest.mark.parametrize('api_location', ['jqdata', 'notebook', 'builtins'])
def test_pasted_notebook_with_numpy_names_previews_writes_and_repeats(backfill, monkeypatch, pollution, api_location):
    """Notebook cells share globals, unlike importing a clean Python module."""
    import sys
    c = backfill
    namespace = {'__name__': 'notebook_cell', 'any': np.any}
    if pollution == 'numpy_namespace':
        namespace.update(all=np.all, min=np.min, max=np.max, bool=np.bool_, int=np.int64)
    exec(compile(SCRIPT.read_text(encoding='utf-8'), str(SCRIPT), 'exec'), namespace)
    namespace.update(china_now=c.module.china_now, ETF_GROUPS=c.module.ETF_GROUPS,
                     connect_database=c.module.connect_database)
    if api_location == 'jqdata':
        monkeypatch.setitem(sys.modules, 'jqdata', c.jq)
    else:
        # The platform can expose finance/query on jqdata but inject market APIs elsewhere.
        monkeypatch.setitem(sys.modules, 'jqdata', SimpleNamespace(finance=c.jq.finance, query=c.jq.query))
        for name in ('get_trade_days', 'get_price', 'get_security_info'):
            if api_location == 'notebook':
                namespace[name] = getattr(c.jq, name)
            else:
                # Ignore stale/non-callable notebook names and resolve the real platform API.
                namespace[name] = None
                monkeypatch.setattr(namespace['_builtins'], name, getattr(c.jq, name), raising=False)
    # Exercise the real API resolver too: it also used an unqualified any(generator).
    preview = namespace['run']()
    assert preview['source_complete'] is True and preview['written'] is False
    assert not c.connections
    written = namespace['run'](write=True)
    assert written['written'] is True
    assert all(count['inserted'] == 4 for count in written['counts'].values())
    repeated = namespace['run'](write=True)
    assert all(count['inserted'] == 0 and count['different_preserved'] == 0
               for count in repeated['counts'].values())
    del c.prices[CODE, DATES[3]]
    incomplete = namespace['run'](write=True)
    assert incomplete['source_complete'] is False and incomplete['written'] is False
    assert len(c.connections) == 2
    for key in ('write', 'allow_partial', 'create_tables'):
        with pytest.raises(namespace['HistoryError'], match='必须是布尔值'):
            namespace['run'](**{key: 1})
    with pytest.raises(namespace['HistoryError'], match='groups'):
        namespace['run'](groups=['unknown'])
    with pytest.raises(namespace['HistoryError'], match='交易日历'):
        namespace['checked_days']([DATES[-1]], DATES[0], DATES[1])


def test_missing_platform_api_names_are_reported_before_collection(backfill, monkeypatch):
    import sys
    import builtins
    namespace = {'__name__': 'notebook_cell'}
    exec(compile(SCRIPT.read_text(encoding='utf-8'), str(SCRIPT), 'exec'), namespace)
    monkeypatch.setitem(sys.modules, 'jqdata', SimpleNamespace(
        finance=backfill.jq.finance, query=backfill.jq.query,
        get_trade_days=backfill.jq.get_trade_days))
    for name in ('get_price', 'get_security_info'):
        monkeypatch.delattr(builtins, name, raising=False)
    with pytest.raises(namespace['HistoryError'], match='get_price, get_security_info'):
        namespace['load_api']()
    assert not backfill.requests and not backfill.connections


def test_persistence_insert_only_preserves_exchange_sources_and_idempotence(backfill):
    c = backfill
    preview = c.module.run()
    old_share = dict(preview['shares'][1], shares=222, source='SSE.ETF_SCALE', updated_at=dt.datetime(2024, 1, 6))
    old_price = dict(preview['prices'][1], close_price=Decimal('3.5'), source='SSE.DAY_KLINE', updated_at=dt.datetime(2024, 1, 6))
    old_day = dict(preview['trading_days'][1], source='SSE.INDEX_TRADING_DAYS', updated_at=dt.datetime(2024, 1, 6))
    c.module.save_records(c.engine, [old_share], [old_price], [old_day])
    first = c.module.run(write=True)
    assert first['written'] is True
    for name, counts in first['counts'].items():
        assert counts['inserted'] == 3 and counts['preserved'] == 1
    for model, expected in ((c.module.ETFShare, old_share), (c.module.ETFPrice, old_price), (c.module.ETFTradingDay, old_day)):
        row = get_rows(c, model)[1]
        assert all(getattr(row, key) == value for key, value in expected.items())
    second = c.module.run(write=True)
    assert all(counts['inserted'] == 0 and counts['preserved'] == 4 for counts in second['counts'].values())
    assert len(get_rows(c, c.module.ETFShare)) == 4


def test_gap_repair_is_not_limited_to_latest_watermark_and_prices_independent(backfill):
    c = backfill
    result = c.module.run()
    c.module.save_records(c.engine, result['shares'][::3], [], result['trading_days'])
    filled = c.module.run(write=True)
    assert filled['counts']['etf_share_daily']['inserted'] == 2
    assert filled['counts']['etf_price_daily']['inserted'] == 4


def test_partial_source_requires_opt_in_and_repeated_run_fills_gap(backfill):
    c = backfill
    del c.prices[CODE, DATES[3]]
    blocked = c.module.run(write=True)
    assert blocked['written'] is False and c.connections == []
    assert blocked['missing']['prices'] == [(CODE, DATES[3])]
    partial = c.module.run(write=True, allow_partial=True)
    assert partial['written'] is True and partial['source_complete'] is False
    assert len(get_rows(c, c.module.ETFPrice)) == 3
    c.prices[CODE, DATES[3]] = Decimal('2')
    complete = c.module.run(write=True)
    assert complete['counts']['etf_price_daily']['inserted'] == 1


def test_listing_date_clips_requests_and_excludes_prelisting_missing(backfill):
    c = backfill
    c.info[CODE].start_date = DATES[3]
    result = c.module.run()
    assert result['source_complete']
    assert [row['trade_date'] for row in result['shares']] == DATES[3:]
    assert all(request[2] == DATES[3] for request in c.requests)
    assert result['reference_missing'] == {'shares': [], 'prices': []}


def test_window_boundaries_and_calendar_context(backfill):
    c = backfill
    c.module.WINDOW_SIZE = 2
    result = c.module.run()
    assert len(c.requests) == 6  # five dates including in-memory reference, in windows of 2/2/1
    assert len(result['shares']) == 4 and result['source_complete']
    assert result['shares'][2]['previous_trade_date'] == DATES[3]


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), -1, '1.5', 2**63, True, 'bad'])
def test_invalid_shares_rejected(backfill, value):
    with pytest.raises(backfill.module.HistoryError):
        backfill.module.normalize_shares(value)


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), 0, -1, '0.123456789', '1000000000000', True, 'bad'])
def test_invalid_close_rejected(backfill, value):
    with pytest.raises(backfill.module.HistoryError):
        backfill.module.normalize_close(value)


def test_share_source_uses_integer_units_without_multiplying_10000(backfill):
    assert backfill.module.normalize_shares('14054616600') == 14054616600
    assert backfill.module.normalize_close('4.304') == Decimal('4.304')


@pytest.mark.parametrize('bad', ['duplicate', 'wrong_code', 'non_session'])
def test_bad_share_response_aborts_before_database(backfill, bad):
    c = backfill
    rows = [dict(code=CODE, date=DATES[2], shares=100)]
    if bad == 'duplicate': rows *= 2
    if bad == 'wrong_code': rows[0]['code'] = '159919.XSHE'
    if bad == 'non_session': rows[0]['date'] = dt.date(2024, 1, 1)
    c.jq.finance.run_query = lambda query: pd.DataFrame(rows)
    with pytest.raises(c.module.HistoryError): c.module.run(write=True)
    assert c.connections == []


def test_duplicate_price_response_aborts_and_paused_not_filled(backfill):
    c = backfill
    c.jq.get_price = lambda *args, **kwargs: pd.DataFrame([{'close': 2, 'paused': 0}] * 2,
                                                        index=pd.DatetimeIndex([DATES[2]] * 2))
    with pytest.raises(c.module.HistoryError): c.module.run(write=True)
    assert c.connections == []
    c.jq.get_price = lambda *args, **kwargs: pd.DataFrame([{'close': 2, 'paused': 1}],
                                                        index=pd.DatetimeIndex([DATES[2]]))
    result = c.module.run()
    assert result['prices'] == [] and not result['source_complete']


def test_calendar_invalid_no_weekday_fallback(backfill):
    c = backfill
    c.jq.get_trade_days = lambda **kwargs: [DATES[3], DATES[2]]
    with pytest.raises(c.module.HistoryError): c.module.run(write=True)
    assert c.connections == []


@pytest.mark.parametrize('kwargs', [{'start_date': '2025-01-01'}, {'end_date': '2024-01-07'},
                                   {'groups': []}, {'groups': ['unknown']}, {'write': 'yes'}])
def test_invalid_run_configuration(backfill, kwargs):
    with pytest.raises(backfill.module.HistoryError): backfill.module.run(**kwargs)
    assert backfill.connections == []


def test_transaction_failure_rolls_back_all_three_tables_without_raw_error(backfill):
    c = backfill
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO etf_price_daily'):
            raise RuntimeError('private user password must never appear')
    event.listen(c.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(c.module.HistoryError) as error:
            c.module.run(write=True)
        assert 'private user password' not in str(error.value)
    finally:
        event.remove(c.engine, 'before_cursor_execute', fail)
    for model in c.module.MODELS:
        assert get_rows(c, model) == []


def test_existing_calendar_conflict_aborts_without_overwriting(backfill):
    c = backfill
    result = c.module.run()
    wrong = dict(result['trading_days'][2], previous_trade_date=DATES[1], source='SSE.INDEX_TRADING_DAYS')
    c.module.save_records(c.engine, [], [], [wrong])
    with pytest.raises(c.module.HistoryError, match='日历'):
        c.module.run(write=True)
    assert get_rows(c, c.module.ETFShare) == []
    assert get_rows(c, c.module.ETFTradingDay)[0].previous_trade_date == DATES[1]


def test_create_tables_requires_explicit_flag(backfill):
    c = backfill
    c.module.ETFPrice.__table__.drop(c.engine)
    with pytest.raises(c.module.HistoryError, match='缺少表'):
        c.module.run(write=True)
    assert c.module.run(write=True, create_tables=True)['written']


def test_boundary_split_price_quarantined_without_2023_writes(backfill):
    c = backfill
    for day in DATES[2:]:
        c.shares[CODE, day] = 200
        c.prices[CODE, day] = Decimal('1')
    result = c.module.run(write=True)
    assert (CODE, DATES[2]) in result['quarantined_prices']
    assert DATES[2] not in [row.trade_date for row in get_rows(c, c.module.ETFPrice)]
    assert all(row.trade_date.year == 2024 for model in c.module.MODELS for row in get_rows(c, model))


def test_in_range_split_matches_web_protection_and_raw_values_retained(backfill, monkeypatch):
    c = backfill
    for day in DATES[3:]:
        c.shares[CODE, day] = 200
        c.prices[CODE, day] = Decimal('1')
    result = c.module.run(write=True)
    assert result['quarantined_prices'] == []
    assert get_rows(c, c.module.ETFShare)[1].shares == 200
    assert get_rows(c, c.module.ETFPrice)[1].close_price == Decimal('1')
    monkeypatch.setattr('app.services.etf.ETF_GROUPS', {'hs300': [(CODE, 'test ETF')]})
    with sessionmaker(bind=c.engine)() as session:
        history = ETFService(ETFRepository(session)).history('hs300', 0, DATES[-1])
    by_day = {row.date: row for row in history.market_values}
    assert by_day[DATES[3]].market_value is None
    assert by_day[DATES[3]].excluded_funds == [CODE]
    assert by_day[DATES[-1]].market_value == '200.00000000'
