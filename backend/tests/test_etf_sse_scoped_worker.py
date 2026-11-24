"""ETF worker, HTTPX streaming, SQL writer and snapshot, without live services."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.models.etf import ETFPrice, ETFShare, ETFTradingDay
from app.services import etf_exchange as exchange
from app.services.etf_collection import CHINA, ETFCollection


BASELINE = date(2026, 11, 19)
HISTORY = date(2026, 11, 20)
TARGET = date(2026, 11, 23)
TODAY = date(2026, 11, 24)
FUNDS = {
    '510300.XSHG': '沪深300ETF华泰柏瑞', '510050.XSHG': '上证50ETF华夏',
    '588000.XSHG': '科创50ETF华夏', '159919.XSHE': '沪深300ETF嘉实',
}
SSE_HISTORY = {'510300': '5518488.77', '510050': '2413416.68', '588000': '10362866.82'}
SSE_TARGET = {'510300': '5558718.77', '510050': '2437896.68', '588000': '10517066.82'}
SSE_TODAY = {'510300': '5558719.77', '510050': '2437897.68', '588000': '10517067.82'}
SZSE_CLOSES = {BASELINE: ('4.239', '4.222'), HISTORY: ('4.191', '4.239'),
               TARGET: ('4.203', '4.191'), TODAY: ('4.250', '4.203')}


def empty_dated_report(error=None):
    report = {'metadata': {'catalogid': '1815_stock_snapshot', 'tabkey': 'tab2', 'pageno': 1,
                           'pagecount': 0, 'recordcount': 0, 'cols': {'ss': '今收', 'qss': '前收'}}, 'data': []}
    if error is not None:
        report['error'] = error
    return httpx.Response(200, json=[report])


class ReadTimeoutBody(httpx.SyncByteStream):
    def __init__(self, request):
        self.request = request

    def __iter__(self):
        yield b'{"result":['
        raise httpx.ReadTimeout('private response body text', request=self.request)


class SourceFixture:
    def __init__(self, tmp_path, monkeypatch, *, manual=False):
        self.now = datetime(2026, 11, 24, 14, 46, tzinfo=CHINA) if manual else datetime(2026, 11, 24, 9, 5, tzinfo=CHINA)
        self.manual = manual
        self.scoped, self.bulk, self.shares_sz, self.quotes, self.price_k, self.refreshes = [], [], [], [], [], []
        self.dated, self.overrides, self.dated_overrides = [], {}, {}
        self.sleeps = []
        owner = self

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.now.astimezone(tz) if tz is not None else owner.now.replace(tzinfo=None)

        monkeypatch.setattr(exchange, 'datetime', FrozenDateTime)
        settings = Settings(_env_file=None, log_dir=tmp_path / 'log', cache_enabled=True, etf_collection_enabled=True)
        self.url = 'sqlite:///' + str(tmp_path / 'business.sqlite')
        self.engine = create_engine(self.url)
        ETFShare.metadata.create_all(self.engine, tables=[ETFShare.__table__, ETFPrice.__table__, ETFTradingDay.__table__])
        self.cache = SnapshotCache(settings, self.engine)

        class Client(exchange.ExchangeClient):
            def __init__(self, http):
                super().__init__(http, sleep=owner.sleeps.append)

        collector = SimpleNamespace(catalog=lambda: FUNDS, ExchangeClient=Client, collect=exchange.collect,
                                    existing_observations=exchange.existing_observations, write_records=exchange.write_records)

        def refresh(**options):
            self.refreshes.append(options)
            return self.cache.refresh_etf_collection(**options)

        self.worker = ETFCollection(settings, refresh=refresh, collector=collector, clock=lambda: self.now,
                                    engine_factory=lambda *_args, **_kwargs: create_engine(self.url),
                                    client_factory=lambda: httpx.Client(transport=httpx.MockTransport(self.handle)))
        self.worker.start = lambda: None

    @staticmethod
    def is_open(day):
        return day.weekday() < 5 and not date(2026, 10, 1) <= day <= date(2026, 10, 7)

    @staticmethod
    def sse_row(code, day, shares):
        return {'SEC_CODE': code, 'STAT_DATE': day.isoformat(), 'TOT_VOL': shares}

    def handle(self, req):
        p = req.url.params
        u = str(req.url.copy_with(query=None))
        today = self.now.date()
        completed = today if self.now.hour >= 15 else today - timedelta(days=1)
        while not self.is_open(completed):
            completed -= timedelta(days=1)
        if u == exchange.SSE_PRICE + '000001':
            start = date(2026, 10, 1)
            days = [start + timedelta(days=offset) for offset in range((completed - start).days + 1)]
            rows = [['19901219']] + [[d.strftime('%Y%m%d')] for d in days if self.is_open(d)]
            return httpx.Response(200, json=dict(code='000001', kline=rows, begin=0, end=len(rows), total=len(rows)))
        if u == exchange.SSE_PRICE.replace('/dayk/', '/snap/') + '000001':
            snap_day = today if self.is_open(today) and self.now.hour >= 9 else completed
            stamp = int(self.now.strftime('%H%M%S')) if snap_day == today else 160055
            day = snap_day.strftime('%Y%m%d')
            return httpx.Response(200, json=dict(code='000001', date=day, snap=[day, stamp]))
        if u == exchange.SZSE_CALENDAR:
            start = date.fromisoformat(p['month'] + '-01')
            following = (start + timedelta(days=32)).replace(day=1)
            days = [start + timedelta(days=offset) for offset in range((following - start).days)]
            return httpx.Response(200, json=dict(nowdate=today.isoformat(),
                data=[dict(jyrq=d.isoformat(), jybz=int(self.is_open(d))) for d in days]))
        if u == exchange.SSE_SHARES:
            day = date.fromisoformat(p['STAT_DATE'])
            if p['sqlId'] == 'COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L':
                self.bulk.append((day, p['pageHelp.pageSize']))
                return httpx.Response(200, stream=ReadTimeoutBody(req))
            assert p['sqlId'] == 'COMMON_SSE_ZQPZ_ETFZL_ETFJBXX_JJGM_SEARCH_L'
            assert p['isPagination'] == 'false'
            code = p['SEC_CODE']
            self.scoped.append((code, day))
            if (code, day) in self.overrides:
                return self.overrides[(code, day)](req)
            values = {BASELINE: SSE_HISTORY, HISTORY: SSE_HISTORY, TARGET: SSE_TARGET, TODAY: SSE_TODAY}[day]
            return httpx.Response(200, json={'result': [self.sse_row(code, day, values[code])],
                                             'pageHelp': {'total': 0}})
        if u == exchange.SZSE_SHARES:
            if p['CATALOGID'] == '1815_stock_snapshot':
                day = date.fromisoformat(p['txtBeginDate'])
                assert p['txtEndDate'] == day.isoformat() and p['txtDMorJC'] == '159919'
                assert p['TABKEY'] == 'tab2' and p['PAGENO'] == '1'
                self.dated.append(day)
                if day in self.dated_overrides:
                    return self.dated_overrides[day](req)
                current, previous = SZSE_CLOSES[day]
                return httpx.Response(200, json=[{
                    'metadata': {'catalogid': '1815_stock_snapshot', 'tabkey': 'tab2', 'pageno': 1,
                                 'pagecount': 1, 'recordcount': 1, 'cols': {'ss': '今收', 'qss': '前收'}},
                    'data': [{'zqdm': '159919', 'jyrq': day.isoformat(), 'ss': current, 'qss': previous}],
                }])
            day = date.fromisoformat(p['txtStart'])
            assert p['txtEnd'] == day.isoformat() and p['txtDm'] == '159919'
            self.shares_sz.append(day)
            return httpx.Response(200, json=[dict(metadata=dict(tabkey='tab1', recordcount=1, pagecount=1, pageno=1),
                data=[dict(fund_code='159919', size_date=day.isoformat(), current_size='2,156,781.66')])])
        if u == exchange.SZSE_PRICE:
            self.price_k.append('159919')
            return httpx.Response(200, json=dict(code='0', data=dict(code='159919',
                picupdata=[['20261120', '4.197', '4.191'], ['20261123', '4.007', '4.203'],
                           ['20261124', '4.210', '4.250']])))
        if u == exchange.SZSE_QUOTE:
            self.quotes.append(p['code'])
            if self.now.hour >= 15:
                tick, close = self.now.isoformat(), '4.250'
            elif self.manual:
                tick, close = self.now.isoformat(), '4.210'
            else:
                tick, close = '2026-11-23 15:15:03', '4.203'
            return httpx.Response(200, json=dict(code='0', datetime=tick,
                data=dict(code='159919', now=close, close='4.203')))
        if u.startswith(exchange.SSE_PRICE):
            code = u.rsplit('/', 1)[1]
            self.price_k.append(code)
            return httpx.Response(200, json=dict(code=code, kline=[
                ['20261119', '4.010'], ['20261120', '4.018'], ['20261123', '4.023'], ['20261124', '4.240']]))
        raise AssertionError(f'Unexpected fixture request {u}')

    def seed_ledger(self, day):
        with self.worker.connect() as db:
            db.executemany("INSERT INTO observations VALUES (?,?,?,'complete',1)", (
                (kind, code, day.isoformat()) for kind in ('shares', 'prices') for code in FUNDS))

    def seed_share(self, code, day, shares):
        with Session(self.engine) as s:
            s.add(ETFShare(fund_code=code, trade_date=day, fund_name=FUNDS[code], shares=shares,
                previous_trade_date=BASELINE if day == HISTORY else HISTORY, source='seed', updated_at=datetime(2026, 11, 23)))
            s.commit()

    def seed_price(self, code, day, close):
        with Session(self.engine) as s:
            s.add(ETFPrice(fund_code=code, trade_date=day, close_price=Decimal(close), source='seed', updated_at=datetime(2026, 11, 23)))
            s.commit()


def test_manual_cutoff_fills_history_and_latest_without_the_timing_out_bulk_path(tmp_path, monkeypatch, caplog):
    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.seed_ledger(BASELINE)
    case.seed_share('510050.XSHG', HISTORY, 12345)  # An existing older value is never re-fetched or overwritten.
    case.seed_share('510300.XSHG', TARGET, 77)      # Latest still accepts a confirmed same-date revision.
    case.seed_price('159919.XSHE', HISTORY, '4.191')
    case.seed_price('159919.XSHE', TARGET, '4.203')
    case.worker.request_run('sse-scoped-latest-manual')
    assert case.worker.tick(case.now)  # The latest target is yesterday, not today's live quote.

    assert case.bulk == []
    assert case.scoped == [('510300', HISTORY), ('588000', HISTORY)] + [(code[:6], TARGET) for code in FUNDS if code.endswith('.XSHG')]
    assert case.shares_sz == [HISTORY, TARGET]
    assert case.price_k.count('510300') == 1 and '159919' not in case.price_k
    assert case.dated == [TARGET] and not case.quotes
    assert case.refreshes == [{'repair_start': BASELINE}]
    state = case.worker.status()
    assert state['manual_gap_start'] is None and not state['cache_pending']
    assert state['metadata']['coverage_end'] == TARGET.isoformat()
    assert state['latest_result']['status'] == 'complete'

    with Session(case.engine) as written, case.cache.session('etf-shares') as local:
        for session in (written, local):
            assert session.get(ETFShare, ('510300.XSHG', HISTORY)).shares == 55_184_887_700
            assert session.get(ETFShare, ('510300.XSHG', TARGET)).shares == 55_587_187_700
            assert session.get(ETFShare, ('510050.XSHG', HISTORY)).shares == 12345
            assert session.get(ETFShare, ('159919.XSHE', TARGET)).shares == 21_567_816_600
            assert session.get(ETFPrice, ('510300.XSHG', TARGET)).close_price == Decimal('4.023')
            assert session.get(ETFPrice, ('159919.XSHE', TARGET)).close_price == Decimal('4.203')
            assert session.get(ETFPrice, ('159919.XSHE', TARGET)).source == 'SZSE.DATED_FUND_CLOSE'
            assert session.get(ETFPrice, ('159919.XSHE', HISTORY)).source == 'seed'
            assert session.get(ETFTradingDay, TARGET).previous_trade_date == HISTORY
            assert session.get(ETFTradingDay, TODAY) is None
    assert 'private response body text' not in caplog.text


@pytest.mark.parametrize('enabled', [True, False])
def test_after_close_manual_does_not_touch_unpublished_day_ahead_of_morning_schedule(tmp_path, monkeypatch, enabled):
    from test_etf_page_configuration import save

    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.now = datetime(2026, 11, 24, 16, 17, 30, tzinfo=CHINA)
    save(case.worker, {'schedule': ['08:00'], 'enabled': enabled})
    case.seed_ledger(BASELINE)
    for code in FUNDS:
        if code.endswith('.XSHG'):
            case.overrides[(code[:6], TODAY)] = lambda _: httpx.Response(200, json={'result': []})
    with case.worker.connect() as db:
        db.execute("INSERT INTO metadata VALUES ('coverage_end',?)", (TODAY.isoformat(),))
        db.execute("INSERT INTO metadata VALUES ('manual_gap_floor',?)", (TODAY.isoformat(),))
        db.executemany("INSERT INTO observations VALUES ('shares','510300.XSHG',?,'retry_exhausted',6)",
                       ((day.isoformat(),) for day in (TARGET, TODAY)))
    case.worker.request_run('manual-published-23-only', retry_exhausted=True)
    with case.worker.connect() as db:
        assert db.execute("SELECT SUM(attempts) FROM observations WHERE status='retry_exhausted'").fetchone()[0] == 12
    assert case.worker.tick(case.now)
    assert {day for _, day in case.scoped} == {HISTORY, TARGET}
    assert case.shares_sz == [HISTORY, TARGET] and case.dated == [HISTORY, TARGET]
    assert not case.quotes and not case.bulk and case.refreshes == [{'repair_start': BASELINE}]
    state = case.worker.status()
    assert state['metadata']['coverage_end'] == TARGET.isoformat()
    assert state['metadata']['manual_gap_floor_deferred:' + TODAY.isoformat()] == TODAY.isoformat()
    assert state['manual_gap_start'] is None and not state['cache_pending']
    assert state['latest_result']['status'] == 'complete' and not state['runs']
    with case.worker.connect() as db:
        row = lambda day: tuple(db.execute("SELECT status,attempts FROM observations "
                                          "WHERE kind='shares' AND code='510300.XSHG' AND day=?",
                                          (day.isoformat(),)).fetchone())
        assert row(TARGET) == ('complete', 1) and row(TODAY) == ('retry_exhausted', 6)
    with Session(case.engine) as written, case.cache.session('etf-shares') as local:
        for session in (written, local):
            assert session.get(ETFShare, ('510300.XSHG', TARGET)).shares == 55_587_187_700
            assert session.get(ETFPrice, ('159919.XSHE', HISTORY)).close_price == Decimal('4.191')
            for code in FUNDS:
                assert session.get(ETFShare, (code, TODAY)) is None
                assert session.get(ETFPrice, (code, TODAY)) is None
            assert session.get(ETFTradingDay, TODAY) is None


@pytest.mark.parametrize(('request_time', 'edited_slot', 'expected'), [
    ((7, 59), '07:00', TARGET), ((8, 0), '09:00', TODAY),
])
def test_manual_requested_before_slot_does_not_expand_at_dequeue_and_exact_slot_includes_previous_session(
        tmp_path, monkeypatch, request_time, edited_slot, expected):
    from test_etf_page_configuration import save

    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.now = datetime(2026, 11, 25, *request_time, tzinfo=CHINA)
    save(case.worker, {'schedule': ['08:00'], 'enabled': False})
    case.seed_ledger(TARGET)
    case.worker.request_run('manual-frozen-threshold')
    case.now = case.now.replace(hour=8, minute=3)  # Same actual execution minute for both requests.
    save(case.worker, {'revision': 1, 'schedule': [edited_slot], 'enabled': False})
    assert case.worker.tick(case.now)
    assert {day for _, day in case.scoped} == {expected}
    assert case.shares_sz == [expected] and case.dated == [expected]
    assert not case.quotes
    state = case.worker.status()
    assert state['metadata']['coverage_end'] == expected.isoformat() and state['latest_result']['status'] == 'complete'
    with Session(case.engine) as written, case.cache.session('etf-shares') as local:
        for session in (written, local):
            assert session.get(ETFShare, ('159919.XSHE', expected)) is not None
            assert session.get(ETFPrice, ('159919.XSHE', expected)).source == 'SZSE.DATED_FUND_CLOSE'
            assert bool(session.get(ETFPrice, ('159919.XSHE', TODAY))) == (expected == TODAY)


def test_weekend_manual_uses_due_friday_slot_but_does_not_reset_or_charge_attempts(tmp_path, monkeypatch):
    from test_etf_page_configuration import save

    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.now = datetime(2026, 11, 21, 16, 0, tzinfo=CHINA)  # Saturday: Friday 08:00 targeted Thursday.
    save(case.worker, {'schedule': ['08:00']})
    case.seed_ledger(BASELINE - timedelta(days=1))
    case.seed_ledger(BASELINE)
    with case.worker.connect() as db:
        db.execute("UPDATE observations SET status='retry_exhausted',attempts=6 WHERE day=? "
                   "AND kind='shares' AND code='510300.XSHG'", (BASELINE.isoformat(),))
    case.overrides[('510300', BASELINE)] = lambda _: httpx.Response(200, json={'result': []})
    case.worker.request_run('weekend-manual-due-only', retry_exhausted=True)
    assert not case.worker.tick(case.now)
    assert {day for _, day in case.scoped} == {BASELINE}
    assert case.shares_sz == [BASELINE] and case.dated == [BASELINE]
    state = case.worker.status()
    assert state['latest_result']['error_details']['trade_date'] == BASELINE.isoformat()
    assert state['retry_exhausted_count'] == 1 and not state['runs']
    assert state['gaps'][0]['attempts'] == 6


def test_after_close_fills_only_missing_older_szse_price_and_keeps_live_same_day_check(tmp_path, monkeypatch):
    from test_etf_page_configuration import save

    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.now = datetime(2026, 11, 24, 15, 32, 55, tzinfo=CHINA)
    save(case.worker, {'schedule': ['15:30']})
    case.seed_ledger(BASELINE)
    for day in (HISTORY, TARGET):
        for code in FUNDS:
            case.seed_share(code, day, 12345)
            if (code, day) != ('159919.XSHE', HISTORY):
                case.seed_price(code, day, '4.203')
    case.worker.request_run('szse-20-historical-price-only')
    complete = case.worker.tick(case.now)
    state = case.worker.status()
    assert complete, state['latest_result']['error_details']

    assert case.dated == [HISTORY]
    assert case.quotes == ['159919'] and case.price_k.count('159919') == 1
    assert all(day == TODAY for _, day in case.scoped)
    assert case.shares_sz == [TODAY]
    assert state['manual_gap_start'] is None and not state['cache_pending']
    assert state['latest_result']['status'] == 'complete'
    assert case.refreshes == [{'repair_start': BASELINE}]
    with Session(case.engine) as written, case.cache.session('etf-shares') as local:
        for session in (written, local):
            missing = session.get(ETFPrice, ('159919.XSHE', HISTORY))
            unchanged = session.get(ETFPrice, ('159919.XSHE', TARGET))
            current = session.get(ETFPrice, ('159919.XSHE', TODAY))
            assert (missing.close_price, missing.source) == (Decimal('4.191'), 'SZSE.DATED_FUND_CLOSE')
            assert (unchanged.close_price, unchanged.source) == (Decimal('4.203'), 'seed')
            assert (current.close_price, current.source) == (Decimal('4.250'), 'SZSE.CLOSE_VERIFIED')


def test_prior_session_empty_report_keeps_committed_price_and_does_not_use_live_quote(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.seed_ledger(TARGET)
    case.seed_price('159919.XSHE', TARGET, '4.203')
    case.dated_overrides[TARGET] = lambda _: empty_dated_report()
    case.worker.request_run('szse-no-dated-candidate-to-write')
    assert not case.worker.tick(case.now)
    assert case.dated == [TARGET] and not case.quotes and '159919' not in case.price_k
    assert case.worker.status()['latest_result']['error_details']['code'] == 'etf_missing_prices'
    with Session(case.engine) as written, case.cache.session('etf-shares') as local:
        for session in (written, local):
            closing = session.get(ETFPrice, ('159919.XSHE', TARGET))
            assert (closing.close_price, closing.source) == (Decimal('4.203'), 'seed')


@pytest.mark.parametrize(('stopped_by', 'code'), [('archive', 'etf_szse_price_archived'), ('rate', 'network_error')])
def test_dated_szse_guard_skips_only_affected_report_requests_not_today_live_price(tmp_path, monkeypatch, stopped_by, code):
    from test_etf_page_configuration import save

    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.now = datetime(2026, 11, 24, 15, 32, 55, tzinfo=CHINA)
    save(case.worker, {'schedule': ['15:30']})
    case.seed_ledger(BASELINE)
    for day in (HISTORY, TARGET):
        for symbol in FUNDS:
            case.seed_share(symbol, day, 12345)
            if symbol.endswith('.XSHG'):
                case.seed_price(symbol, day, '4.203')
    if stopped_by == 'archive':
        # A simulated published boundary covers both pending older dates.
        case.dated_overrides[HISTORY] = lambda _: empty_dated_report('归档平台|2026-11-24 00:00:00.0')
    else:
        case.dated_overrides[HISTORY] = lambda _: httpx.Response(429)
    case.worker.request_run('szse-dated-guard-isolation')
    assert not case.worker.tick(case.now)

    assert case.dated == [HISTORY] and case.quotes == ['159919']
    assert case.shares_sz == [TODAY]
    state = case.worker.status()
    assert state['manual_gap_start'] == HISTORY.isoformat() and not state['cache_pending']
    details = state['latest_result']['error_details']
    assert details['code'] == code and details['trade_date'] == HISTORY.isoformat()
    assert (details.get('http_status') == 429) == (stopped_by == 'rate')
    with Session(case.engine) as written, case.cache.session('etf-shares') as local:
        for session in (written, local):
            assert session.get(ETFPrice, ('159919.XSHE', HISTORY)) is None
            assert session.get(ETFPrice, ('159919.XSHE', TARGET)) is None
            assert session.get(ETFPrice, ('159919.XSHE', TODAY)).close_price == Decimal('4.250')


def test_automatic_scope_keeps_same_day_revision_and_does_not_walk_manual_gap(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch)
    case.seed_ledger(BASELINE)
    case.seed_share('510300.XSHG', TARGET, 77)
    with case.worker.connect() as db:
        db.execute("INSERT INTO metadata VALUES ('manual_gap_floor',?)", (HISTORY.isoformat(),))
    assert case.worker.tick(case.now)

    assert case.bulk == [] and case.sleeps == []
    assert case.scoped == [(code[:6], TARGET) for code in FUNDS if code.endswith('.XSHG')]
    assert case.shares_sz == [TARGET]
    assert case.dated == [TARGET] and not case.quotes
    assert case.refreshes == [{}]
    state = case.worker.status()
    assert state['manual_gap_start'] == HISTORY.isoformat()
    assert state['metadata']['coverage_end'] == TARGET.isoformat()
    assert state['latest_result']['status'] == 'complete' and state['gap_count'] == 0
    with case.cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', TARGET)).shares == 55_587_187_700
        assert local.get(ETFShare, ('159919.XSHE', TARGET)).source == 'SZSE.FUND_SCALE'
        assert local.get(ETFPrice, ('159919.XSHE', TARGET)).source == 'SZSE.DATED_FUND_CLOSE'
        assert local.get(ETFShare, ('510300.XSHG', HISTORY)) is None


def test_automatic_empty_is_not_retried_and_429_leaves_unattempted_keys_pending(tmp_path, monkeypatch, caplog):
    import logging

    case = SourceFixture(tmp_path, monkeypatch)
    case.seed_share('510300.XSHG', TARGET, 77)
    case.overrides[('510300', TARGET)] = lambda _: httpx.Response(200, json={'result': [], 'pageHelp': {'total': 0}})
    case.overrides[('510050', TARGET)] = lambda _: httpx.Response(429, json={'text': 'redact this private rate message'})
    with caplog.at_level(logging.INFO):
        assert not case.worker.tick(case.now)

    assert case.bulk == [] and case.sleeps == []
    assert case.scoped == [('510300', TARGET), ('510050', TARGET)]
    assert case.shares_sz == [TARGET]
    state = case.worker.status()
    assert state['latest_result']['status'] == 'partial'
    assert state['latest_result']['error_details']['http_status'] == 429
    assert state['latest_result']['error_details']['trade_date'] == TARGET.isoformat()
    assert {gap['code']: gap['attempts'] for gap in state['gaps']} == {
        '510300.XSHG': 1, '510050.XSHG': 1, '588000.XSHG': 0}
    assert 'skipped_sse_rate_limit=1' in caplog.text
    assert 'redact this private rate message' not in caplog.text
    with case.cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', TARGET)).shares == 77
        assert local.get(ETFShare, ('588000.XSHG', TARGET)) is None
        assert local.get(ETFPrice, ('588000.XSHG', TARGET)).close_price == Decimal('4.023')
        assert local.get(ETFShare, ('159919.XSHE', TARGET)).shares == 21_567_816_600

    case.overrides.clear()
    case.now += timedelta(minutes=30)
    assert case.worker.tick(case.now)
    assert case.scoped[-3:] == [(code[:6], TARGET) for code in FUNDS if code.endswith('.XSHG')]
    assert case.worker.status()['gap_count'] == 0


def test_manual_rate_limit_in_history_does_not_send_more_on_the_latest_branch(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.seed_ledger(BASELINE)
    case.overrides[('510050', HISTORY)] = lambda _: httpx.Response(429)
    case.worker.request_run('sse-scoped-rate-limit-manual')
    assert not case.worker.tick(case.now)

    assert case.scoped == [('510050', HISTORY)] and case.bulk == []
    assert case.shares_sz == [HISTORY, TARGET]
    state = case.worker.status()
    assert state['manual_gap_start'] == HISTORY.isoformat()
    assert state['latest_result']['error_details']['http_status'] == 429
    assert all(gap['attempts'] == 0 for gap in state['gaps'] if gap['kind'] == 'shares')
    with case.cache.session('etf-shares') as local:
        for day in (HISTORY, TARGET):
            assert local.get(ETFShare, ('510300.XSHG', day)) is None
            assert local.get(ETFShare, ('159919.XSHE', day)).shares == 21_567_816_600


def test_cancelled_during_latest_response_stops_remaining_calls_and_publication(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    case.seed_ledger(TARGET)
    request_id = 'sse-scoped-cancel-during-body'
    case.worker.request_run(request_id)
    closed = []

    class CancelDuringBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"result":'
            case.worker.request_cancel(request_id)
            yield b'[]}'

        def close(self):
            closed.append(True)

    case.overrides[('510300', TARGET)] = lambda _: httpx.Response(200, stream=CancelDuringBody())
    assert not case.worker.tick(case.now)
    assert closed == [True]
    assert case.scoped == [('510300', TARGET)] and case.bulk == []
    assert not case.shares_sz and not case.refreshes
    assert case.worker.status()['latest_result']['status'] == 'cancelled'
    with Session(case.engine) as written:
        assert written.get(ETFShare, ('510300.XSHG', TARGET)) is None


def test_business_commit_failure_does_not_replay_or_publish_scoped_rows(tmp_path, monkeypatch):
    import pymysql
    from sqlalchemy.exc import OperationalError

    case = SourceFixture(tmp_path, monkeypatch)
    case.seed_share('510300.XSHG', TARGET, 77)
    engine = create_engine(case.url)
    commits = []

    @event.listens_for(engine, 'commit')
    def disconnect(_):
        commits.append(True)
        if len(commits) == 2:  # Table existence checks precede the business transaction.
            raise OperationalError(None, None, pymysql.err.OperationalError(2013, 'private transport trace'))

    case.worker.engine_factory = lambda *args, **kwargs: engine
    assert not case.worker.tick(case.now)
    assert commits == [True, True]
    assert case.bulk == [] and len(case.scoped) == 3
    state = case.worker.status()
    assert state['latest_result']['error_details']['code'] == 'etf_commit_unconfirmed'
    assert state['latest_result']['error_details']['mysql_errno'] == 2013
    assert not case.refreshes and not state['cache_pending']
    case.now += timedelta(minutes=30)
    assert not case.worker.tick(case.now)
    assert len(case.scoped) == 3 and commits == [True, True]
    with Session(case.engine) as written:
        assert written.get(ETFShare, ('510300.XSHG', TARGET)).shares == 77
        assert written.get(ETFShare, ('159919.XSHE', TARGET)) is None


def test_scoped_manual_commit_republishes_after_real_cache_lock_without_refetch(tmp_path, monkeypatch):
    from test_etf_page_configuration import save

    case = SourceFixture(tmp_path, monkeypatch)
    save(case.worker, {'enabled': False})
    case.seed_ledger(TARGET)
    case.worker.request_run('sse-scoped-republish-disabled')
    lock = case.cache.acquire()
    assert lock is not None
    try:
        assert case.worker.tick(case.now)
        state = case.worker.status()
        assert state['cache_pending']
        assert state['latest_result']['error_details']['code'] == 'publication_pending'
        with Session(case.engine) as written:
            assert written.get(ETFShare, ('510300.XSHG', TARGET)).shares == 55_587_187_700
        assert case.scoped == [(code[:6], TARGET) for code in FUNDS if code.endswith('.XSHG')]
    finally:
        lock.release()

    case.worker.client_factory = lambda: pytest.fail('pending publication must not contact the exchange')
    case.now += timedelta(minutes=5)
    assert not case.worker.tick(case.now)
    assert not case.worker.status()['cache_pending']
    assert not case.worker.status()['runs']  # Auto-disabling remains effective.
    assert case.refreshes == [{}, {}]
    with case.cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', TARGET)).shares == 55_587_187_700
        assert local.get(ETFPrice, ('159919.XSHE', TARGET)).close_price == Decimal('4.203')
