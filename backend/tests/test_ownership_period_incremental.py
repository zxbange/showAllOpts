"""Fund disclosures must not expand into unrelated quarters or shareholder scans."""
import copy
import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import Column, Date, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression

from test_ownership_collector import mod


OLD = dt.date(2025, 6, 30)
RECENT = [dt.date(2025, 12, 31), dt.date(2026, 3, 31),
          dt.date(2026, 6, 30), dt.date(2026, 9, 30)]
Q2, Q3 = RECENT[-2:]
CODES = ['600001.XSHG', '600002.XSHG', '600003.XSHG', '600004.XSHG']
A, B, C, D = CODES


def constrained(call, field):
    """Return equality/IN scope, independent of generated bind parameter names."""
    result = set()
    for expression in visitors.iterate(call.statement.whereclause):
        if not isinstance(expression, BinaryExpression):
            continue
        if getattr(expression.left, 'name', None) != field:
            continue
        if expression.operator not in (operators.eq, operators.in_op):
            continue
        value = getattr(expression.right, 'value', None)
        result.update(value if isinstance(value, (list, tuple, set)) else [value])
    return result


@pytest.fixture
def collector(mod, monkeypatch):
    base = declarative_base()

    class Fund(base):
        __tablename__ = 'source_fund'
        id = Column(Integer, primary_key=True)
        code = Column(String)
        symbol = Column(String)
        period_end = Column(Date)
        pub_date = Column(Date)
        report_type_id = Column(Integer)
        report_type = Column(String)
        shares = Column(Float)
        market_cap = Column(Float)

    class Floats(base):
        __tablename__ = 'source_floats'
        id = Column(Integer, primary_key=True)
        code = Column(String)
        end_date = Column(Date)
        pub_date = Column(Date)
        change_reason_id = Column(Integer)
        sharesnature_id = Column(Integer)
        shareholder_rank = Column(Integer)
        shareholder_id = Column(Integer)
        shareholder_name = Column(String)
        shareholder_class_id = Column(Integer)
        share_number = Column(Float)
        share_ratio = Column(Float)

    class Holder(base):
        __tablename__ = 'source_holders'
        id = Column(Integer, primary_key=True)
        code = Column(String)
        end_date = Column(Date)
        pub_date = Column(Date)
        share_holders = Column(Integer)
        a_share_holders = Column(Integer)
        b_share_holders = Column(Integer)
        h_share_holders = Column(Integer)

    class Main(base):
        __tablename__ = 'source_main'
        id = Column(Integer, primary_key=True)
        main_code = Column(String)
        statistics_main_code = Column(String)

    source = create_engine('sqlite://')
    target = create_engine('sqlite://')
    base.metadata.create_all(source)
    source_session = sessionmaker(bind=source)()
    target_session = sessionmaker(bind=target)
    state = SimpleNamespace(now=dt.datetime(2026, 10, 25, 16), fail=None)
    calls, prices = [], []

    def run_query(query):
        call = SimpleNamespace(
            statement=query.statement,
            tables={table.name for table in query.statement.get_final_froms()},
            columns={column.name for column in query.statement.selected_columns})
        calls.append(call)
        if state.fail is not None and state.fail(call):
            raise RuntimeError('synthetic incremental source failure')
        with source.connect() as connection:
            result = connection.execute(query.statement)
            return pd.DataFrame(result.fetchall(), columns=result.keys())

    def get_price(codes, **kwargs):
        prices.append((list(codes), kwargs))
        return pd.DataFrame({'code': list(codes), 'close': [2.] * len(codes)})

    @contextmanager
    def scope(Base, config):
        Base.metadata.create_all(target)
        yield sessionmaker(bind=target)

    monkeypatch.setattr(mod, 'database_scope', scope)
    monkeypatch.setattr(mod, 'china_now', lambda: state.now)
    mod.STRATEGY_CONFIG['个股机构持仓'].update(
        history_years=2, refresh_quarters=4, stock_batch_size=2, page_size=7)
    finance = SimpleNamespace(FUND_MAIN_INFO=Main, FUND_PORTFOLIO_STOCK=Fund,
        STK_SHAREHOLDER_FLOATING_TOP10=Floats, STK_HOLDER_NUM=Holder, run_query=run_query)
    apis = dict(finance=finance, query=source_session.query,
        get_all_securities=lambda **kw: pd.DataFrame(
            {'display_name': ['Stock A', 'Stock B', 'Stock C', 'Stock D']}, index=CODES),
        get_industry=lambda codes, **kw: {code: {'sw_l2': {'industry_name': 'industry'}} for code in codes},
        get_trade_days=lambda **kw: [kw['end_date']], get_price=get_price)
    ns = mod._ownership_strategy(apis, mod.DB_CONFIG)

    def add_fund(fund, code, period, shares, pub=None, full=None):
        if full is None:
            full = period.month in (6, 12)
        source_session.add(Fund(code=fund, symbol=code.split('.')[0], period_end=period,
            pub_date=pub or period + dt.timedelta(days=20), shares=shares, market_cap=shares * 2,
            report_type_id=403005 if full else 403003, report_type='半年度' if full else '第三季度'))
        source_session.commit()

    def add_floats(code, period, social_shares=100, pub=None):
        for rank in range(1, 11):
            source_session.add(Floats(code=code, end_date=period,
                pub_date=pub or period + dt.timedelta(days=20), change_reason_id=306019,
                sharesnature_id=308007, shareholder_rank=rank, shareholder_id=rank,
                shareholder_name='全国社保基金五零二组合' if rank == 1 else 'company' + str(rank),
                shareholder_class_id=307004 if rank == 1 else 307099,
                share_number=social_shares if rank == 1 else 100, share_ratio=1))
        source_session.commit()

    def snapshots():
        with target_session() as session:
            return {(row.code, row.report_date): (copy.deepcopy(row.payload), row.updated_at)
                    for row in session.query(ns.OwnershipReport).all()}

    def probe():
        with target_session() as session:
            row = session.query(ns.DisclosureProbe).filter_by(key='main').one()
            return copy.deepcopy(row.payload), row.as_of

    def journal():
        with target_session() as session:
            return session.query(ns.SyncHead).filter_by(id='main').one().revision

    source_session.add_all([
        Main(main_code='P', statistics_main_code='P'),
        Main(main_code='C', statistics_main_code='P'),
        Main(main_code='U', statistics_main_code='U')])
    source_session.commit()
    # P's older-quarter constituent C must not join a Q3-only update.
    for period in [OLD] + RECENT[:-1]:
        add_fund('P', C, period, 100)
    add_fund('P', A, Q3, 100)
    add_fund('C', A, Q3, 100)
    add_fund('C', D, Q3, 80)  # Non-selected share class must not reappear.
    add_fund('U', A, Q3, 20)
    add_fund('U', B, Q3, 50)
    for period in [OLD] + RECENT:
        for code in CODES:
            add_floats(code, period, 200 if period == Q3 else 100)
    source_session.add(Holder(code=A, end_date=Q3, pub_date=dt.date(2026, 10, 20),
        share_holders=100, a_share_holders=100))
    source_session.commit()
    assert not ns.run().empty
    state.now = dt.datetime(2026, 10, 26, 16)
    calls.clear()
    prices.clear()
    result = SimpleNamespace(ns=ns, run=ns.run, add_fund=add_fund, add_floats=add_floats,
        calls=calls, prices=prices, state=state, snapshots=snapshots, probe=probe, journal=journal,
        source=source_session, Fund=Fund, Floats=Floats, Holder=Holder, target_session=target_session,
        apis=apis)
    try:
        yield result
    finally:
        source_session.close()
        source.dispose()
        target.dispose()


def later_calls(collector, table):
    return [call for call in collector.calls[6:] if table in call.tables]


def assert_quiet_repeat(collector):
    before, revision = collector.snapshots(), collector.journal()
    collector.calls.clear()
    collector.prices.clear()
    assert collector.run().empty
    assert len(collector.calls) == 6
    assert collector.prices == []
    assert collector.snapshots() == before
    assert collector.journal() == revision


@pytest.mark.parametrize('changed_share_class', ['P', 'C'])
def test_fund_change_only_refreshes_changed_quarter_and_keeps_other_sources(collector, changed_share_class):
    c = collector
    before = c.snapshots()
    c.add_fund(changed_share_class, B, Q3, 75, pub=c.state.now.date())
    # P exits A; U's unchanged contribution must survive A's recomputation.
    assert not c.run().empty
    after = c.snapshots()
    for key, value in before.items():
        if key not in ((A, Q3), (B, Q3)):
            assert after[key] == value
    for code, shares, count in ((A, 20, 1), (B, 125, 2)):
        previous, current = before[(code, Q3)][0], after[(code, Q3)][0]
        assert current['categories']['public_fund']['shares'] == shares
        assert current['categories']['public_fund']['holder_count'] == count
        assert {k: v for k, v in current.items() if k != 'categories'} == {
            k: v for k, v in previous.items() if k != 'categories'}
        assert {k: v for k, v in current['categories'].items() if k != 'public_fund'} == {
            k: v for k, v in previous['categories'].items() if k != 'public_fund'}
    assert after[(D, Q3)][0]['categories']['public_fund'] is None
    assert not later_calls(c, 'source_floats')
    assert not later_calls(c, 'source_holders')
    assert not c.prices
    fund_scopes = [constrained(call, 'period_end') for call in later_calls(c, 'source_fund')]
    assert {Q3} in fund_scopes
    assert all(scope <= {Q3} for scope in fund_scopes)
    assert_quiet_repeat(c)


def test_fund_exit_clears_only_public_aggregate(collector):
    c = collector
    before = c.snapshots()
    # U's new document contains B only, then P also exits A.
    c.add_fund('U', B, Q3, 50, pub=c.state.now.date())
    c.add_fund('P', B, Q3, 75, pub=c.state.now.date())
    c.run()
    current = c.snapshots()[(A, Q3)][0]
    assert current['categories']['public_fund'] is None
    assert current['float_members'] == before[(A, Q3)][0]['float_members']
    assert current['categories']['social'] == before[(A, Q3)][0]['categories']['social']
    assert current['social_entry'] == before[(A, Q3)][0]['social_entry']
    assert_quiet_repeat(c)


def test_float_disclosure_reviews_four_quarters_without_fund_reads(collector):
    c = collector
    before = c.snapshots()
    c.add_floats(A, Q3, social_shares=350, pub=c.state.now.date())
    assert not c.run().empty
    after = c.snapshots()
    assert after[(A, Q3)][0]['categories']['social']['shares'] == 350
    for key, (payload, stamp) in before.items():
        assert after[key][0]['categories']['public_fund'] == payload['categories']['public_fund']
        if key != (A, Q3):
            assert after[key] == (payload, stamp)
    assert not later_calls(c, 'source_fund')
    assert not later_calls(c, 'source_main')
    assert not later_calls(c, 'source_holders')
    scope = set().union(*(constrained(call, 'end_date') for call in later_calls(c, 'source_floats')))
    assert scope == set(RECENT)
    assert not c.prices  # Floating disclosures use shares, without valuation requests.
    assert_quiet_repeat(c)


def test_mixed_sources_keep_separate_period_and_stock_scopes(collector):
    c = collector
    before = c.snapshots()
    c.add_fund('P', B, Q3, 75, pub=c.state.now.date())
    c.add_floats(C, Q3, social_shares=450, pub=c.state.now.date())
    c.run()
    after = c.snapshots()
    assert after[(A, Q3)][0]['categories']['public_fund']['shares'] == 20
    assert after[(B, Q3)][0]['categories']['public_fund']['shares'] == 125
    assert after[(C, Q3)][0]['categories']['social']['shares'] == 450
    for key, value in before.items():
        if key not in ((A, Q3), (B, Q3), (C, Q3)):
            assert after[key] == value
    assert all(constrained(call, 'period_end') <= {Q3}
               for call in later_calls(c, 'source_fund'))
    floats = later_calls(c, 'source_floats')
    assert set().union(*(constrained(call, 'end_date') for call in floats)) == set(RECENT)
    assert all(constrained(call, 'code') <= {C} for call in floats)
    assert not c.prices
    assert_quiet_repeat(c)


def test_late_fund_disclosure_updates_old_period_within_retained_history(collector):
    c = collector
    before = c.snapshots()
    c.add_fund('P', C, OLD, 175, pub=c.state.now.date())
    c.run()
    after = c.snapshots()
    assert after[(C, OLD)][0]['categories']['public_fund']['shares'] == 175
    assert {key: value for key, value in after.items() if key != (C, OLD)} == {
        key: value for key, value in before.items() if key != (C, OLD)}
    assert all(constrained(call, 'period_end') <= {OLD}
               for call in later_calls(c, 'source_fund'))
    assert not later_calls(c, 'source_floats') and not c.prices
    assert_quiet_repeat(c)


def test_failed_later_quarter_keeps_probe_and_retries_without_losing_committed_changes(collector):
    c = collector
    before, old_probe = c.snapshots(), c.probe()
    c.add_fund('P', C, Q2, 175, pub=c.state.now.date())
    c.add_fund('P', B, Q3, 75, pub=c.state.now.date())
    c.state.fail = lambda call: ('source_fund' in call.tables and 'shares' in call.columns
                                and constrained(call, 'period_end') == {Q3})
    with pytest.raises(RuntimeError, match='synthetic incremental source failure'):
        c.run()
    partial = c.snapshots()
    assert partial[(C, Q2)][0]['categories']['public_fund']['shares'] == 175
    assert partial[(A, Q3)] == before[(A, Q3)]
    assert partial[(B, Q3)] == before[(B, Q3)]
    assert c.probe() == old_probe
    c.state.fail = None
    c.calls.clear()
    c.run()
    after = c.snapshots()
    assert after[(C, Q2)] == partial[(C, Q2)]
    assert after[(A, Q3)][0]['categories']['public_fund']['shares'] == 20
    assert after[(B, Q3)][0]['categories']['public_fund']['shares'] == 125
    assert c.probe() != old_probe
    assert_quiet_repeat(c)


def test_new_fund_only_stock_stays_unknown_until_shareholder_disclosure(collector):
    c = collector
    code = '600005.XSHG'
    original = c.apis['get_all_securities']
    c.apis['get_all_securities'] = lambda **kw: pd.concat([
        original(**kw), pd.DataFrame({'display_name': ['New stock']}, index=[code])])
    c.add_fund('P', code, Q3, 75, pub=c.state.now.date())
    c.run()
    current = c.snapshots()[(code, Q3)][0]
    fund = copy.deepcopy(current['categories']['public_fund'])
    assert fund['shares'] == 75 and fund['holder_count'] == 1
    assert current['float_members'] == []
    assert current['float_complete'] is False
    assert current.get('social_complete', False) is False
    assert current['social_entry']['status'] == 'unknown'
    assert not later_calls(c, 'source_floats') and not c.prices
    c.add_floats(code, Q3, social_shares=450, pub=c.state.now.date())
    c.calls.clear()
    c.run()
    current = c.snapshots()[(code, Q3)][0]
    assert current['categories']['public_fund'] == fund
    assert current['float_complete'] and current['social_complete']
    assert current['categories']['social']['shares'] == 450
    assert current['social_entry']['status'] == 'unknown'  # No prior-quarter list exists.
    assert not later_calls(c, 'source_fund') and not later_calls(c, 'source_main')
    assert_quiet_repeat(c)


def test_non_a_share_new_constituent_still_removes_the_old_a_share_holding(collector):
    c = collector
    before = c.snapshots()
    c.add_fund('P', '999999.XSHG', Q3, 75, pub=c.state.now.date())
    c.run()
    after = c.snapshots()
    # New constituent is out of scope; P's removal from A remains significant.
    assert after[(A, Q3)][0]['categories']['public_fund']['shares'] == 20
    assert all(code != '999999.XSHG' for code, period in after)
    assert {key: value for key, value in after.items() if key != (A, Q3)} == {
        key: value for key, value in before.items() if key != (A, Q3)}
    assert not later_calls(c, 'source_floats') and not c.prices
    assert_quiet_repeat(c)
