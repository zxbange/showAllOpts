"""Committed ETF group summaries through the real writer, worker, and local outbox."""
from datetime import datetime, timedelta

import pytest
import pymysql
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.etf_catalog import ETF_GROUPS, GROUP_NAMES
from app.models.etf import ETFShare
from app.services import collection_notifications as notifications, etf_exchange as exchange
from app.services.etf_notifications import ETFRunSummary, ETFShareComparison

from test_etf_sse_scoped_worker import BASELINE, FUNDS, HISTORY, TARGET, SourceFixture


def _funds(group):
    return [code for code, _ in ETF_GROUPS[group]]


def _rows(day, units):
    return [dict(fund_code=code, trade_date=day, fund_name=code, shares=value,
                 previous_trade_date=day - timedelta(days=1),
                 source='fixture', updated_at=datetime(2026, 11, 24)) for code, value in units.items()]


def _is_comparison(sql):
    # The existing writer SELECT includes full working records, not just two columns.
    return sql.startswith('SELECT etf_share_daily.fund_code, etf_share_daily.shares')


def test_exact_prior_session_complete_fixed_rosters_and_signed_units_in_one_read_after_commit():
    current = {code: 10_000 for members in ETF_GROUPS.values() for code, _ in members}
    previous = current.copy()
    current[_funds('hs300')[0]] += 1
    current[_funds('zz500')[0]] = 0
    not_fresh = _funds('kc50')[1]
    no_prior = _funds('zz1000')[1]
    current.pop(not_fresh)
    previous.pop(no_prior)

    engine = create_engine('sqlite:///:memory:')
    exchange.write_records(engine, [
        *_rows(BASELINE, {no_prior: 123}),
        *_rows(HISTORY, previous),
        *_rows(TARGET, {not_fresh: 9_999}),  # Stored target value does not repair a fresh source gap.
        *_rows(TARGET + timedelta(days=1), {no_prior: 456}),
    ], [])
    calls, checkouts = [], []
    event.listen(engine, 'commit', lambda *_: calls.append('commit'))
    event.listen(engine, 'checkout', lambda *_: checkouts.append(1))

    @event.listens_for(engine, 'before_cursor_execute')
    def compare(_conn, _cursor, sql, params, _context, _multi):
        if _is_comparison(sql):
            calls.append('comparison')
            assert str(HISTORY) in params
            assert str(BASELINE) not in params and str(TARGET) not in params
            assert len(params) <= len(current) + 1 and not_fresh not in params

    report = ETFShareComparison(TARGET, HISTORY)
    assert exchange.write_records(engine, _rows(TARGET, current), [], share_comparison=report) == len(current)
    assert calls == ['commit', 'commit', 'comparison'] and checkouts == [1]
    lines = report.lines()
    assert lines[0] == '份额对比：2026-11-20 → 2026-11-23（单位：万份）'
    assert [line.split('：', 1)[0] for line in lines[1:]] == list(GROUP_NAMES.values())
    rows = dict(line.split('：', 1) for line in lines[1:])
    assert rows['沪深300 ETF'] == '当日 5.0001；前交易日 5；增减 +0.0001'
    assert rows['中证500 ETF'] == '当日 4；前交易日 5；增减 -1'
    assert rows['创业板 ETF'] == '当日 1；前交易日 1；增减 0'
    assert '当日缺 1/2 只（588080），无法计算增减' in rows['科创50 ETF']
    assert '前交易日缺 1/3 只（159845），无法计算增减' in rows['中证1000 ETF']
    assert rows['上证180 ETF'] == '当日 1；前交易日 1；增减 0'


@pytest.mark.parametrize('has_current', [True, False])
def test_optional_comparison_failure_never_invalidates_committed_shares_or_leaks_raw_error(caplog, has_current):
    engine = create_engine('sqlite:///:memory:')
    code = _funds('cyb')[0]
    exchange.write_records(engine, _rows(HISTORY, {code: 10_000}), [])
    calls = []

    @event.listens_for(engine, 'before_cursor_execute')
    def fail(_conn, _cursor, sql, _params, _context, _multi):
        if _is_comparison(sql):
            calls.append(1)
            raise RuntimeError('private DB source')

    report = ETFShareComparison(TARGET, HISTORY)
    assert exchange.write_records(engine, _rows(TARGET, {code: 20_000} if has_current else {}),
                                  [], share_comparison=report) == int(has_current)
    assert calls == ([1] if has_current else [])
    with Session(engine) as written:
        share = written.get(ETFShare, (code, TARGET))
        assert (share.shares if share else None) == (20_000 if has_current else None)
    assert 'private DB source' not in caplog.text
    if has_current:
        assert '创业板 ETF：当日 2；前交易日未核验，无法计算增减' in report.lines()


def test_run_report_keeps_current_evidence_separate_from_history_and_commit_certainty():
    report = ETFRunSummary(True, target=TARGET, coverage=(17, 18, 18),
        historical_gaps={('shares', '510300.XSHG', BASELINE), ('prices', '159919.XSHE', HISTORY)},
        committed_changes=3, commit_unconfirmed=True)
    assert report.lines() == [
        '目标日来源核验：份额 17/18 只，收盘价 18/18 只',
        '历史待补：2 个交易日，份额 1 项，收盘价 1 项，最早 2026-11-19',
        '此前已确认变更 3 条；另有提交结果待确认',
    ]


def _enable_outbox(monkeypatch):
    monkeypatch.setattr(notifications, 'configured_channels', lambda *_: ('telegram',))
    monkeypatch.setattr(notifications, 'start_notifications', lambda *_: None)


def test_no_change_automatic_and_manual_share_same_group_schema_real_worker_and_outbox(tmp_path, monkeypatch):
    _enable_outbox(monkeypatch)
    case = SourceFixture(tmp_path, monkeypatch)
    case.seed_ledger(TARGET)
    code = _funds('sz50')[0]
    case.seed_share(code, HISTORY, 24_000_000_000)
    with case.worker.client_factory() as http:
        client = exchange.ExchangeClient(http)
        calendar = client.calendar()
        shares, prices, _ = exchange.collect(client, TARGET, TARGET, funds=FUNDS,
            calendar=calendar, scoped_sse=True, dated_szse_prices=True)
    assert exchange.write_records(case.engine, shares, prices, calendar=calendar, calendar_target=TARGET) == 8
    assert case.worker.tick(case.now)
    case.now += timedelta(minutes=6)
    case.worker.request_run('etf-identical-manual')
    assert case.worker.tick(case.now)
    with notifications._database(case.worker.settings) as outbox:
        auto, manual = outbox.execute('SELECT run,message FROM outbox ORDER BY rowid').fetchall()
    assert auto['run'] != manual['run'] and manual['run'] == 'manual:etf-identical-manual'
    assert '触发方式：自动采集' in auto['message'] and '触发方式：手动采集' in manual['message']
    for message in (auto['message'], manual['message']):
        assert '采集结果：成功（无新增或变更）' in message and '更新记录：0' in message
        assert '目标数据日期：2026-11-23' in message
        assert '目标日来源核验：份额 4/4 只，收盘价 4/4 只' in message
        assert '上证50 ETF：当日 2,437,896.68；前交易日 2,400,000；增减 +37,896.68' in message
        assert all(f'{group}：' in message for group in GROUP_NAMES.values())
    business = lambda message: [line for line in message.splitlines()
                               if not line.startswith(('触发方式', '采集时间', '历史：'))]
    assert business(auto['message']) == business(manual['message'])


@pytest.mark.parametrize('manual', [False, True])
def test_cache_pending_has_same_partial_terminal_and_no_duplicate_when_published(tmp_path, monkeypatch, manual):
    from test_etf_page_configuration import save

    _enable_outbox(monkeypatch)
    case = SourceFixture(tmp_path, monkeypatch, manual=manual)
    case.seed_ledger(TARGET)
    case.worker.refresh = lambda **_: False
    if manual:
        save(case.worker, {'enabled': False, 'schedule': ['09:00']})
        case.worker.request_run('etf-cache-pending')
    assert case.worker.tick(case.now)
    assert case.worker.status()['latest_result']['status'] == 'partial'
    case.worker.refresh = lambda **_: True
    case.now += timedelta(minutes=6)
    assert not case.worker.tick(case.now)
    with notifications._database(case.worker.settings) as outbox:
        result, = outbox.execute('SELECT message FROM outbox').fetchall()
    assert '采集结果：部分完成' in result['message']
    assert '数据尚未完整发布到页面缓存' in result['message']
    assert '目标数据日期：2026-11-23' in result['message']


def test_verified_cache_outcome_recovers_same_run_without_second_notification(tmp_path, monkeypatch):
    from app.services.collection_outcomes import latest_outcomes

    _enable_outbox(monkeypatch)
    case = SourceFixture(tmp_path, monkeypatch)
    case.worker.cache_publisher = case.cache.publish_etf_collection
    case.worker.cache_verifier = case.cache.verify_etf_publication
    held = case.cache.acquire()
    assert held is not None
    try:
        assert case.worker.tick(case.now)
        original = case.worker.status()['latest_result']
        assert original['status'] == 'partial'
    finally:
        held.release()
    case.now += timedelta(minutes=6)
    assert not case.worker.tick(case.now)
    result = case.worker.status()['latest_result']
    assert result['status'] == 'complete' and result['id'] == original['id']
    assert latest_outcomes(case.worker.settings)['items']['etf-shares']['outcome'] == 'success'
    with notifications._database(case.worker.settings) as outbox:
        messages = outbox.execute('SELECT message FROM outbox').fetchall()
    assert len(messages) == 1 and '采集结果：部分完成' in messages[0]['message']


def test_ambiguous_commit_never_reports_totals_or_a_false_zero(tmp_path, monkeypatch):
    _enable_outbox(monkeypatch)
    case = SourceFixture(tmp_path, monkeypatch)
    case.seed_ledger(TARGET)
    engine = create_engine(case.url)
    calls = []
    case.worker.engine_factory = lambda *_a, **_k: engine

    @event.listens_for(engine, 'commit')
    def disconnect(_):
        calls.append('commit')
        if len(calls) == 2:
            raise OperationalError(None, None, pymysql.err.OperationalError(2013, 'private transport error'))

    @event.listens_for(engine, 'before_cursor_execute')
    def compare(_conn, _cursor, sql, _params, _context, _multi):
        if _is_comparison(sql):
            calls.append('comparison')

    assert not case.worker.tick(case.now)
    assert calls == ['commit', 'commit']
    with notifications._database(case.worker.settings) as outbox:
        result, = outbox.execute('SELECT message FROM outbox').fetchall()
    message = result['message']
    assert '采集结果：失败' in message and '更新记录：未核实' in message
    assert '提交结果未确认' in message and 'private transport error' not in message
    assert '增减 0' not in message and '当日已入库份额未核实' in message


def test_without_opt_in_no_comparison_sql_or_outbox(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch)
    engine = create_engine(case.url)
    case.worker.engine_factory = lambda *_a, **_k: engine
    monkeypatch.setattr(notifications, 'start_notifications', lambda *_: pytest.fail('disabled outbox must not start'))

    @event.listens_for(engine, 'before_cursor_execute')
    def compare(_conn, _cursor, sql, _params, _context, _multi):
        assert not _is_comparison(sql)

    assert case.worker.tick(case.now)
    assert not (case.worker.settings.log_path / 'collection-notifications' / 'outbox.sqlite3').exists()
