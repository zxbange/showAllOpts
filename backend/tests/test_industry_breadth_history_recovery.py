"""Offline JoinQuant contracts and SQLite transactions, not a live source/backfill claim."""
import ast
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]


def load(name, monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    spec = importlib.util.spec_from_file_location('history_' + name, ROOT / 'collectors' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source():
    days = [day.date() for day in pd.bdate_range('2020-01-02', periods=42)]
    codes = ['000001.XSHE', '000002.XSHE']
    state = dict(fail=None, missing=False, pool_before=None, called=[], member_bad=False,
                 calendar_short=False, bad_st=False)

    def securities(**kw):
        day = dt.date.fromisoformat(str(kw['date'])[:10])
        selected = codes[:1] if state['pool_before'] is not None and day < state['pool_before'] else codes
        return pd.DataFrame(dict(start_date=[dt.date(2010, 1, 1)] * len(selected),
                                 end_date=[dt.date(2200, 1, 1)] * len(selected)), index=selected)

    def calendar(start_date=None, end_date=None, count=None):
        end = dt.date.fromisoformat(str(end_date)[:10])
        available = [day for day in days if day <= end]
        if count is not None:
            return available[-count:]
        start = dt.date.fromisoformat(str(start_date)[:10])
        found = [day for day in available if day >= start]
        return found[1:] if state['calendar_short'] else found

    def price(batch, start_date=None, end_date=None, count=None, **kw):
        day = dt.date.fromisoformat(str(end_date)[:10])
        state['called'].append(day)
        if state['fail'] == day:
            raise RuntimeError('offline injected API failure')
        window = calendar(start_date, end_date, count)
        rows = [dict(code=code, time=value, close=(10 * 1.02 ** days.index(value) if code == codes[0] else 10))
                for code in batch for value in window]
        if state['missing']:
            rows.pop()
        return pd.DataFrame(rows)

    def st(name, batch, start_date, end_date, df):
        return pd.DataFrame({code: [None if state['bad_st'] else False] for code in batch}, index=[end_date])

    apis = dict(get_all_trade_days=lambda: days, get_trade_days=calendar,
                get_all_securities=securities, get_price=price, get_extras=st,
                get_industries=lambda **kw: pd.DataFrame({'name': ['A', 'B'],
                    'start_date': [dt.date(2000, 1, 1)] * 2}, index=['A', 'B']),
                get_industry_stocks=lambda code, **kw: [codes[0] if code == 'A' else codes[1]],
                get_industry=lambda batch, **kw: {code: {'sw_l2': {'industry_code': 'A' if code == codes[0] else 'B'}}
                    for code in (batch[:-1] if state['member_bad'] else batch)},
                query=lambda *a: SimpleNamespace(filter=lambda codes: codes),
                valuation=SimpleNamespace(code=SimpleNamespace(in_=lambda codes: codes), market_cap='market_cap'),
                get_fundamentals=lambda batch, date: pd.DataFrame([dict(code=code, market_cap=i+1) for i, code in enumerate(batch)]))
    return SimpleNamespace(days=days, codes=codes, state=state, apis=apis)


@pytest.fixture
def industry(monkeypatch, tmp_path, source):
    m = load('行业动量计算_数据库版.py', monkeypatch)
    for key, value in source.apis.items():
        setattr(m, key, value)
    engine = create_engine('sqlite:///' + str(tmp_path / 'industry.db'))
    monkeypatch.setattr(m.MysqlMomentum, 'connect_to_mysql', lambda: engine)
    m.Base.metadata.create_all(engine)
    yield SimpleNamespace(m=m, engine=engine, Session=sessionmaker(bind=engine), source=source)
    engine.dispose()


@pytest.fixture
def breadth(monkeypatch, tmp_path, source):
    m = load('微盘新高新低数量统计_数据库版.py', monkeypatch)
    engine = create_engine('sqlite:///' + str(tmp_path / 'breadth.db'))
    monkeypatch.setattr(m, 'connect_database', lambda config: engine)
    ns = m._breadth_strategy(source.apis, m.DB_CONFIG)
    ns.Base.metadata.create_all(engine)
    yield SimpleNamespace(m=m, ns=ns, engine=engine, Session=sessionmaker(bind=engine), source=source)
    engine.dispose()


def rows(Session, model):
    session = Session()
    try:
        return session.query(model).count()
    finally:
        session.close()


def journal_payloads(engine):
    with engine.connect() as connection:
        return [json.loads(row[0]) for row in connection.execute(text('SELECT payload FROM result_sync_change'))]


def test_industry_earliest_history_prefix_resume_and_no_retention(industry):
    c = industry
    c.m.cleanup_old_data = lambda *a, **kw: pytest.fail('History cannot call retention')
    for _ in range(5):
        c.m.run_history(end_date=c.source.days[23], max_days=5)
    assert rows(c.Session, c.m.IndustryMomentum) == 8
    with c.Session() as session:
        assert session.query(c.m.MomentumHistoryStatus).filter_by(status='unavailable_prefix').count() == 20
        assert session.query(c.m.MomentumHistoryStatus).filter_by(status='complete').count() == 4
    c.source.state['called'].clear()
    assert len(c.m.run_history(end_date=c.source.days[23])) == 0
    assert c.source.state['called'] == []
    assert all(change['table'] == 'industry_momentum' for payload in journal_payloads(c.engine) for change in payload)


def test_industry_repairs_older_date_and_partial_day_without_max_skip(industry):
    c = industry
    start, middle, end = c.source.days[21:24]
    c.m.run_history(start_date=start, end_date=end)
    with c.Session() as session:
        original = session.query(c.m.IndustryMomentum).filter_by(trade_date=middle, industry_code='A').one()
        original_updated = original.updated_at
        session.query(c.m.IndustryMomentum).filter_by(trade_date=start).delete()
        session.query(c.m.IndustryMomentum).filter_by(trade_date=middle, industry_code='B').delete()
        session.commit()
    assert len(c.m.run_history(start_date=start, end_date=end)) == 3
    with c.Session() as session:
        assert session.query(c.m.IndustryMomentum).count() == 6
        assert session.query(c.m.IndustryMomentum).filter_by(trade_date=middle, industry_code='A').one().updated_at == original_updated


def test_industry_source_failure_not_completed_and_existing_values_protected(industry):
    c = industry
    start, end = c.source.days[21:23]
    c.source.state['fail'] = end
    with pytest.raises(RuntimeError):
        c.m.run_history(start_date=start, end_date=end)
    assert rows(c.Session, c.m.IndustryMomentum) == 2
    assert rows(c.Session, c.m.MomentumHistoryStatus) == 1
    c.source.state['fail'] = None
    c.m.run_history(start_date=start, end_date=end)
    with c.Session() as session:
        row = session.query(c.m.IndustryMomentum).filter_by(trade_date=start, industry_code='A').one()
        row.industry_name = 'preserve legacy value'
        session.commit()
    with pytest.raises(ValueError, match='冲突'):
        c.m.run_history(start_date=start, end_date=end)
    with c.Session() as session:
        assert session.query(c.m.IndustryMomentum).filter_by(trade_date=start, industry_code='A').one().industry_name == 'preserve legacy value'


@pytest.mark.parametrize('fault', ['missing', 'member_bad'])
def test_industry_source_completeness_is_required(industry, fault):
    c = industry
    c.source.state[fault] = True
    with pytest.raises(ValueError):
        c.m.run_history(start_date=c.source.days[-1], end_date=c.source.days[-1])
    assert rows(c.Session, c.m.IndustryMomentum) == rows(c.Session, c.m.MomentumHistoryStatus) == 0


def test_industry_no_unavailable_after_first_success_even_requested_suffix(industry):
    c = industry
    c.m.run_history(start_date=c.source.days[21], end_date=c.source.days[21])
    def unavailable(*a, **kw):
        raise c.m.MomentumHistoryUnavailable('verified_listing_warmup_150_calendar_days')
    c.m.calculate_momentum_history = unavailable
    with pytest.raises(ValueError, match='成功历史'):
        c.m.run_history(start_date=c.source.days[22], end_date=c.source.days[22])
    assert rows(c.Session, c.m.MomentumHistoryStatus) == 1


def test_breadth_prefix_pool_insufficient_is_not_source_failure(breadth):
    c = breadth
    c.source.state['pool_before'] = c.source.days[32]
    for _ in range(7):
        c.ns.run_history(end_date=c.source.days[34], micro_num=2, max_days=5)
    assert rows(c.Session, c.ns.MicrocapBreadth) == 3
    with c.Session() as session:
        reasons = [row.reason for row in session.query(c.ns.BreadthHistoryStatus).filter_by(status='unavailable_prefix')]
        assert reasons.count('verified_calendar_warmup_31') == 30
        assert reasons.count('verified_insufficient_pool_1_of_2') == 2
    assert all(change['table'] == 'microcap_breadth' for payload in journal_payloads(c.engine) for change in payload)
    c.source.state['called'].clear()
    assert len(c.ns.run_history(end_date=c.source.days[34], micro_num=2)) == 0
    assert not c.source.state['called']


def test_breadth_source_error_and_st_missing_not_marked_unavailable(breadth):
    c = breadth
    day = c.source.days[31]
    c.source.state['fail'] = day
    with pytest.raises(RuntimeError):
        c.ns.run_history(start_date=day, end_date=day, micro_num=2)
    assert rows(c.Session, c.ns.BreadthHistoryStatus) == 0
    c.source.state['fail'] = None
    c.source.state['bad_st'] = True
    with pytest.raises(ValueError):
        c.ns.run_history(start_date=day, end_date=day, micro_num=2)
    assert rows(c.Session, c.ns.MicrocapBreadth) == rows(c.Session, c.ns.BreadthHistoryStatus) == 0


def test_breadth_hole_resume_preserves_existing_and_journals_normal_mode(breadth):
    c = breadth
    start, middle, end = c.source.days[32:35]
    c.ns.run_history(start_date=start, end_date=end, micro_num=2)
    with c.Session() as session:
        original = session.query(c.ns.MicrocapBreadth).filter_by(trade_date=end).one().updated_at
        session.query(c.ns.MicrocapBreadth).filter_by(trade_date=middle).delete()
        session.commit()
    c.source.state['called'].clear()
    assert len(c.ns.run_history(start_date=start, end_date=end, micro_num=2)) == 1
    assert c.source.state['called'] == [middle]
    c.ns.run(start_date=start, end_date=c.source.days[35], micro_num=2)
    with c.Session() as session:
        assert session.query(c.ns.MicrocapBreadth).filter_by(trade_date=end).one().updated_at == original
    assert len(journal_payloads(c.engine)) == 5


def test_breadth_insufficient_pool_after_success_is_failure(breadth):
    c = breadth
    first, second = c.source.days[32:34]
    c.ns.run_history(start_date=first, end_date=first, micro_num=2)
    c.source.state['pool_before'] = c.source.days[35]
    with pytest.raises(ValueError, match='成功历史'):
        c.ns.run_history(start_date=second, end_date=second, micro_num=2)
    assert rows(c.Session, c.ns.BreadthHistoryStatus) == 1


@pytest.mark.parametrize('kind', ['industry', 'breadth'])
def test_truncated_calendar_fails_before_connect(request, kind, monkeypatch):
    c = request.getfixturevalue(kind)
    c.source.state['calendar_short'] = True
    if kind == 'industry':
        monkeypatch.setattr(c.m.MysqlMomentum, 'connect_to_mysql', lambda: pytest.fail('Must not connect'))
        call = c.m.run_history
    else:
        monkeypatch.setattr(c.m, 'connect_database', lambda config: pytest.fail('Must not connect'))
        call = c.ns.run_history
    with pytest.raises(ValueError, match='日历'):
        call(end_date=c.source.days[-1])


@pytest.mark.parametrize('kind', ['industry', 'breadth'])
def test_receipt_and_journal_roll_back_with_data(request, kind):
    c = request.getfixturevalue(kind)
    day = c.source.days[-1]
    class FailCommit(c.Session.class_):
        def commit(self):
            self.flush()
            raise RuntimeError('injected commit failure')
    failing = sessionmaker(bind=c.engine, class_=FailCommit)
    if kind == 'industry':
        records = c.m.calculate_momentum_history(day, 'sw_l2', c.source.days)
        with pytest.raises(RuntimeError):
            c.m.save_momentum_history(failing, day, 'sw_l2', records=records)
        model, status = c.m.IndustryMomentum, c.m.MomentumHistoryStatus
    else:
        record = c.ns.calculate_day(day, 2, history_window=c.source.days[-31:])
        with pytest.raises(RuntimeError):
            c.ns.save_history_day(failing, day, 2, record=record)
        model, status = c.ns.MicrocapBreadth, c.ns.BreadthHistoryStatus
    assert rows(c.Session, model) == rows(c.Session, status) == 0
    assert journal_payloads(c.engine) == []
    with c.engine.connect() as connection:
        assert connection.execute(text('SELECT revision FROM result_sync_head')).scalar() == 0


def test_industry_normal_cleanup_preserves_certified_history_and_journals_deletes(industry):
    c = industry
    day, other = c.source.days[21:23]
    c.m.run_history(start_date=day, end_date=day)
    c.m.save_day(c.Session, other, 'sw_l2', c.m.calculate_momentum_history(other, 'sw_l2', c.source.days))
    assert c.m.cleanup_old_data(c.Session, c.source.days[-1], c.m.MomentumHistoryStatus) == 2
    with c.Session() as session:
        assert {row.trade_date for row in session.query(c.m.IndustryMomentum)} == {day}
    assert all(change['deleted'] for change in journal_payloads(c.engine)[-1])


def test_python36_and_embedded_journal_parity():
    canonical = (ROOT / 'collectors/rules/result_journal.py').read_text()
    start, end = '# BEGIN SHARED RESULT JOURNAL', '# END SHARED RESULT JOURNAL'
    expected = canonical[canonical.index(start):canonical.index(end)]
    for name in ['行业动量计算_数据库版.py', '微盘新高新低数量统计_数据库版.py']:
        source = (ROOT / 'collectors' / name).read_text()
        ast.parse(source, feature_version=(3, 6))
        assert source[source.index(start):source.index(end)] == expected
