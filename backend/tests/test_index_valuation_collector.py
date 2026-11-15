"""Offline end-to-end coverage of the standalone and embedded v2 valuation collector."""
import ast
import datetime as dt
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from test_collector_batch import CONFIG, load

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'collectors/估值标尺历史补采_聚宽研究版.py'
DAYS = [dt.date(2026, 1, n) for n in (5, 6, 7)]


@pytest.fixture
def collector(monkeypatch, tmp_path):
    jq = ModuleType('jqdata')
    monkeypatch.setitem(sys.modules, 'jqdata', jq)
    m = load(PATH, 'index_valuation_v2')
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    monkeypatch.setitem(sys.modules, 'mysql_connector', SimpleNamespace(create_mysql_engine=lambda: engine))
    calls, failures = [], {}
    def index_stocks(index, date):
        calls.append(('members', index, date))
        return ['{:06d}.XSHG'.format(600000+i) for i in range(m.INDEXES[index][1])]
    jq.get_index_stocks = index_stocks
    jq.valuation = SimpleNamespace(code=SimpleNamespace(in_=lambda codes: codes), day=object(),
        pe_ratio=object(), pe_ratio_lyr=object(), market_cap=object())
    jq.query = lambda *args: SimpleNamespace(filter=lambda codes: codes)
    def fundamentals(codes, date):
        calls.append(('valuation', date, len(codes)))
        if failures.get('day') == date:
            raise RuntimeError('mysql://user:secret@private')
        return pd.DataFrame(dict(code=codes, day=[date]*len(codes), pe_ratio=[10]*len(codes),
                                 pe_ratio_lyr=[20]*len(codes), market_cap=[100]*len(codes)))
    jq.get_fundamentals = fundamentals
    def price(index, **kw):
        assert kw['start_date'] == kw['end_date'] and kw['fq'] is None
        return pd.DataFrame({'close': [4000]}, index=[kw['end_date']])
    jq.get_price = price
    jq.get_all_trade_days = lambda: DAYS
    jq.get_trade_days = lambda **kw: [d for d in DAYS if kw['start_date'] <= d <= kw['end_date']]
    return SimpleNamespace(m=m, engine=engine, jq=jq, calls=calls, failures=failures)


def sample(m, index='000300.XSHG'):
    return [dict(code='{:06d}.XSHG'.format(600000+i), pe_ttm=Decimal(10), pe_lyr=Decimal(20),
                 market_cap=Decimal(100), suspended_listing=False) for i in range(m.INDEXES[index][1])]


def run(c, **kw):
    return c.m.run(start_date=DAYS[0], end_date=DAYS[-1], **kw)


def test_three_indices_six_metrics_and_repeat_without_source_reads(collector):
    c = collector
    result = run(c)
    assert len(result) == 9
    for row in result:
        assert row['member_count'] == c.m.INDEXES[row['index_code']][1]
        assert [row[x+'_pe_ttm'] for x in ('overall', 'mean', 'median')] == [Decimal(10)]*3
        assert [row[x+'_pe_lyr'] for x in ('overall', 'mean', 'median')] == [Decimal(20)]*3
    c.calls.clear()
    assert run(c) == []
    assert c.calls == []
    with c.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM valuation_ruler_daily_v2')).scalar() == 9
        # 4,050 stock observations stored as 9 audit payloads.
        assert db.execute(text('SELECT count(*) FROM valuation_ruler_member_v2')).scalar() == 9
        assert db.execute(text('SELECT count(*) FROM result_sync_change')).scalar() == 9


def test_threshold_inclusive_and_overall_keeps_losses_high_pe(collector):
    m = collector.m
    rows = [dict(code=str(i), suspended_listing=False, pe_ttm=str(pe), market_cap=str(cap))
            for i, (pe, cap) in enumerate([(10, 100), (20, 400), (-10, 10), (300, 300), (600, 600)])]
    overall, mean, median, stats = m.aggregate(rows, 'pe_ttm')
    # Overall includes losses and >300, with market-cap weighting; arithmetic does not.
    assert overall == Decimal('45.483870967742')  # 1410 / (10+20-1+1+1)
    assert mean == Decimal(110) and median == Decimal(20)
    assert (stats['eligible'], stats['loss'], stats['over_limit']) == (3, 1, 1)
    rows[0]['suspended_listing'] = True
    _, mean, median, stats = m.aggregate(rows, 'pe_ttm')
    assert mean == median == Decimal(160) and stats['suspended_listing'] == 1


def test_missing_values_separate_samples_and_nonpositive_profit(collector):
    m = collector.m
    members = sample(m)
    members[0].update(pe_ttm=None, pe_lyr=Decimal(600))
    members[1].update(pe_ttm=Decimal(-3), pe_lyr=None)
    daily, raw = m.summarize(DAYS[0], '4000', members)
    stats = json.loads(daily['statistics'])
    assert daily['overall_pe_ttm'] is None and daily['overall_pe_lyr'] is None
    assert (stats['ttm']['eligible'], stats['ttm']['loss'], stats['ttm']['missing_pe']) == (298, 1, 1)
    assert (stats['lyr']['eligible'], stats['lyr']['over_limit'], stats['lyr']['missing_pe']) == (298, 1, 1)
    assert json.loads(raw['payload'])[0]['pe_ttm'] is None
    assert daily['members_sha256'] == hashlib.sha256(raw['payload'].encode()).hexdigest()
    assert m.aggregate([dict(pe_ttm='-1', market_cap='100', suspended_listing=False)], 'pe_ttm')[:3] == (None, None, None)
    assert m.source_number(float('nan'), 'PE') is None


@pytest.mark.parametrize('field,bad', [('pe_ttm', 0), ('pe_ttm', 'Infinity'), ('pe_lyr', 'NaN'),
    ('market_cap', -1), ('market_cap', True), ('pe_ttm', '1.0000000000001')])
def test_invalid_raw_values_rejected_before_commit(collector, field, bad):
    m = collector.m
    rows = sample(m)
    rows[0][field] = bad
    with pytest.raises(m.HistoryError):
        m.summarize(DAYS[0], '4000', rows)


def test_resume_each_index_old_version_untouched_and_max_days_means_calendar_days(collector):
    c = collector
    with c.engine.begin() as db:
        db.execute(text('CREATE TABLE valuation_ruler_daily (trade_date TEXT, marker TEXT)'))
        db.execute(text("INSERT INTO valuation_ruler_daily VALUES ('2099-01-01','keep')"))
    assert len(run(c, indices=['000300.XSHG'], max_days=2)) == 2
    report = run(c, max_days=1, return_report=True)
    assert (report['added'], report['remaining']) == (2, 5)
    c.calls.clear()
    assert len(run(c)) == 5
    assert ('members', '000300.XSHG', DAYS[0]) not in c.calls
    with c.engine.connect() as db:
        assert db.execute(text('SELECT marker FROM valuation_ruler_daily')).scalar() == 'keep'


def test_failed_day_resumes_and_prior_successes_remain(collector):
    c = collector
    c.failures['day'] = DAYS[1]
    with pytest.raises(RuntimeError) as caught:
        run(c)
    assert 'secret' not in str(caught.value)
    assert str(DAYS[1]) in str(caught.value)
    with c.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM valuation_ruler_daily_v2')).scalar() == 3
    c.failures.clear()
    c.calls.clear()
    assert len(run(c)) == 6
    assert not any(call[0] == 'members' and call[2] == DAYS[0] for call in c.calls)


def test_write_failure_rolls_back_summary_payload_and_journal(collector):
    c = collector
    def fail(*args):
        raise RuntimeError('private_password')
    event.listen(c.m.ValuationMember, 'before_insert', fail)
    with pytest.raises(RuntimeError) as caught:
        run(c)
    assert 'private_password' not in str(caught.value)
    with c.engine.connect() as db:
        for table in ('valuation_ruler_daily_v2', 'valuation_ruler_member_v2', 'result_sync_change'):
            assert db.execute(text('SELECT count(*) FROM '+table)).scalar() == 0
    event.remove(c.m.ValuationMember, 'before_insert', fail)
    assert len(run(c)) == 9


@pytest.mark.parametrize('corrupt', ['payload', 'summary', 'missing'])
def test_resume_boundary_validation(collector, corrupt):
    c = collector
    run(c, indices=['000300.XSHG'], max_days=1)
    with c.engine.begin() as db:
        if corrupt == 'payload':
            db.execute(text("UPDATE valuation_ruler_member_v2 SET payload='[]'"))
        elif corrupt == 'summary':
            db.execute(text('UPDATE valuation_ruler_daily_v2 SET mean_pe_ttm=11'))
        else:
            db.execute(text('DELETE FROM valuation_ruler_member_v2'))
    c.calls.clear()
    with pytest.raises((RuntimeError, c.m.HistoryError)):
        run(c, indices=['000300.XSHG'])
    assert c.calls == []


@pytest.mark.parametrize('fault', ['duplicate', 'wrong_day', 'missing_field', 'missing_rows', 'bad_price_day'])
def test_source_coverage_guards(collector, fault):
    c = collector
    original = c.jq.get_fundamentals
    def fundamentals(codes, date):
        frame = original(codes, date)
        if fault == 'duplicate': frame.loc[0, 'code'] = frame.loc[1, 'code']
        if fault == 'wrong_day': frame.loc[0, 'day'] = DAYS[1]
        if fault == 'missing_field': frame = frame.drop(columns=['pe_ratio_lyr'])
        if fault == 'missing_rows': frame = frame.iloc[:-1]
        return frame
    c.jq.get_fundamentals = fundamentals
    if fault == 'bad_price_day':
        c.jq.get_price = lambda *args, **kw: pd.DataFrame({'close': [4000]}, index=[DAYS[1]])
    with pytest.raises(c.m.HistoryError):
        c.m.source_day(DAYS[0])


def test_preview_is_read_only_python36_and_rounding(collector, monkeypatch, capsys):
    c = collector
    ast.parse(PATH.read_text(encoding='utf-8'), feature_version=(3, 6))
    monkeypatch.setitem(sys.modules, 'mysql_connector', None)
    assert len(run(c, write_database=False, max_days=1)) == 3
    assert '3/3' in capsys.readouterr().out
    assert c.m.source_number('1.1234567890125', 'PE') == Decimal('1.123456789012')
    assert c.m.source_number('-1.1234567890135', 'PE') == Decimal('-1.123456789014')


def test_default_full_range_and_index_launch_boundary(collector):
    c = collector
    days = [dt.date(2014, 10, 16), dt.date(2014, 10, 17)]
    c.jq.get_all_trade_days = lambda: days
    c.jq.get_trade_days = lambda **kw: days
    result = c.m.run(start_date=days[0], end_date=days[-1])
    assert len(result) == 5
    assert ('members', '000852.XSHG', days[0]) not in c.calls


def test_batch_embeds_identical_source_and_runs_all_indices(collector, monkeypatch):
    c = collector
    monkeypatch.setitem(sys.modules, 'mysql_connector', SimpleNamespace(
        get_database_config=lambda: CONFIG, get_database_options=lambda: {},
        mysql_drivername=lambda _: 'mysql+pymysql', engine_options=lambda _: {}))
    batch = load(ROOT / 'collectors/策略批量采集_数据库版.py', 'batch_valuation_v2')
    monkeypatch.setattr(batch, 'connect_database', lambda _: c.engine)
    results = batch.run_all(['估值标尺'], options={'估值标尺': dict(start_date=DAYS[0], end_date=DAYS[-1])},
        db_config=CONFIG, api_namespace=vars(c.jq), preview_rows=0)
    assert results[0]['status'] == 'SUCCESS' and results[0]['rows'] == 9
    assert batch.run_all(['估值标尺'], options={'估值标尺': dict(start_date=DAYS[0], end_date=DAYS[-1])},
        db_config=CONFIG, api_namespace=vars(c.jq), preview_rows=0)[0]['rows'] == 0
    sync = load(ROOT / 'collectors/tools/sync_history_strategies.py', 'sync_valuation_v2')
    sync.sync(check=True)
