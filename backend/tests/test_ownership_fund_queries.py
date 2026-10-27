"""Fund detail queries must exclude obsolete documents before downloading rows."""
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import Column, Date, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from test_ownership_collector import mod


PERIOD = dt.date(2026, 6, 30)
AS_OF = dt.date(2026, 10, 26)
CODE = '600001.XSHG'


@pytest.fixture
def source(mod):
    base = declarative_base()

    class Fund(base):
        __tablename__ = 'fund_selected_query'
        id = Column(Integer, primary_key=True)
        code = Column(String)
        symbol = Column(String)
        period_end = Column(Date)
        pub_date = Column(Date)
        report_type_id = Column(Integer)
        report_type = Column(String)
        shares = Column(Float)
        market_cap = Column(Float)

    engine = create_engine('sqlite://')
    base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    calls, downloads = [], []
    cap = [None]

    def run_query(query):
        if cap[0]:
            query = query.limit(cap[0])
        calls.append(query.statement)
        with engine.connect() as connection:
            result = connection.execute(query.statement)
            frame = pd.DataFrame(result.fetchall(), columns=result.keys())
        if 'shares' in frame.columns:
            downloads.extend(frame.to_dict('records'))
        return frame

    ns = mod._ownership_strategy(dict(finance=SimpleNamespace(
        FUND_PORTFOLIO_STOCK=Fund, run_query=run_query), query=session.query), mod.DB_CONFIG)

    def add(identity, fund, symbol='600001', pub=dt.date(2026, 8, 20),
            report_type=403005, name='半年度', shares=10, period=PERIOD):
        session.add(Fund(id=identity, code=fund, symbol=symbol, period_end=period,
                         pub_date=pub, report_type_id=report_type, report_type=name,
                         shares=shares, market_cap=None if shares is None else shares * 2))

    yield SimpleNamespace(ns=ns, session=session, Fund=Fund, add=add,
                          calls=calls, downloads=downloads, cap=cap)
    session.close()
    engine.dispose()


def test_only_final_documents_are_downloaded_and_exited_stock_is_not_revived(source):
    s = source
    s.add(1, 'A', pub=dt.date(2026, 7, 20), report_type=403002, name='第二季度', shares=9000)
    s.add(2, 'A', symbol='600002', shares=20)  # preferred A report drops the requested stock
    s.add(3, 'C', shares=8888)  # same document date, but A is the canonical share class
    s.add(4, 'B', shares=10)
    s.add(5, 'B', shares=15)  # final duplicate in exactly the same report wins
    s.add(6, 'B', pub=dt.date(2026, 9, 1), report_type=403002, name='第二季度', shares=9999)
    s.add(7, 'F.OF', shares=7)  # physical source code must survive exact SQL filtering
    s.add(8, 'UNRELATED', symbol='600099', shares=7777)
    s.add(9, 'B', pub=AS_OF + dt.timedelta(days=1), shares=9999)
    s.add(10, 'B', period=dt.date(2026, 3, 31), shares=9999)
    s.add(11, 'B', report_type=403002, name='第二季度', shares=9999)
    s.session.commit()
    s.cap[0] = 1  # an upstream cap must not truncate the selected reports or detail rows
    fund_map = {'A': 'A', 'C': 'A', 'F': 'F'}
    selected = s.ns.select_documents(PERIOD, AS_OF, fund_map, [CODE])
    assert set(selected) == {'A', 'B', 'F'}
    assert selected['A'][1][0] == 'A'
    assert selected['B'][1][1:] == (dt.date(2026, 8, 20), 403005)
    assert selected['F'][1][0] == 'F.OF'
    s.calls.clear()
    totals = s.ns.batch_funds([CODE], PERIOD, AS_OF, fund_map, selected)
    assert totals[CODE]['shares'] == 22
    assert totals[CODE]['market_value'] == 44
    assert totals[CODE]['holder_count'] == 2
    assert {row['id'] for row in s.downloads} == {4, 5, 7}
    assert all('report_type_id =' in str(statement) and 'code IN' in str(statement)
               for statement in s.calls)


def test_missing_numeric_values_stay_unknown_and_empty_selection_skips_source(source):
    s = source
    s.add(1, 'A', shares=None)
    s.add(2, 'B', shares=12)
    s.session.commit()
    selected = s.ns.select_documents(PERIOD, AS_OF, {}, [CODE])
    totals = s.ns.batch_funds([CODE], PERIOD, AS_OF, {}, selected)
    assert totals[CODE]['holder_count'] == 2
    assert totals[CODE]['shares'] is None and totals[CODE]['market_value'] is None
    s.calls.clear()
    assert s.ns.batch_funds([CODE], PERIOD, AS_OF, {}, {}) == {}
    assert s.ns.batch_funds([], PERIOD, AS_OF, {}, selected) == {}
    assert not s.calls


def test_scalar_index_batches_are_independent_of_stock_batch_size(source):
    s = source
    s.ns.settings['stock_batch_size'] = 1
    for identity in range(1, 602):
        s.add(identity, 'F{:04d}'.format(identity))
    s.session.commit()
    selected = s.ns.select_documents(PERIOD, AS_OF, {}, [CODE])
    assert len(selected) == 601
    version_queries = [statement for statement in s.calls
                       if len(statement.selected_columns) == 5]
    assert len(version_queries) == 6  # three groups, each followed by its empty page
    sizes = [len(statement.compile().params['code_1']) for statement in version_queries]
    assert sizes == [300, 300, 300, 300, 1, 1]
    assert [len(batch) for batch in s.ns.index_batches(list(range(601)))] == [300, 300, 1]
    s.calls.clear()
    totals = s.ns.batch_funds([CODE], PERIOD, AS_OF, {}, selected)
    assert totals[CODE]['holder_count'] == 601
    assert len(s.calls) == 2  # one grouped document predicate, one final empty page
    assert len(s.downloads) == 601


@pytest.mark.parametrize('many_dates', [False, True])
def test_document_predicates_are_bounded_and_cover_every_document_once(source, many_dates):
    s = source
    size = 257 if many_dates else 8001
    selected = {}
    for index in range(size):
        fund = 'F{:05d}'.format(index)
        pub = AS_OF - dt.timedelta(days=index if many_dates else 0)
        selected[fund] = ((), (fund, pub, 403005))
    predicates = list(s.ns.fund_document_conditions(s.Fund, selected))
    assert len(predicates) == (3 if many_dates else 2)
    covered = []
    for predicate in predicates:
        params = predicate.compile().params
        code_groups = [value for key, value in params.items() if key.startswith('code_')]
        codes = [code for group in code_groups for code in group]
        assert len(codes) <= 8000 and len(code_groups) <= 128
        covered.extend(codes)
    assert len(covered) == len(set(covered)) == size
    assert set(covered) == set(selected)
