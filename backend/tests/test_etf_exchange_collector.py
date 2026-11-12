import importlib.util
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.etf import ETFShare, ETFPrice, ETFTradingDay
from app.services.holdings_calendar import trading_day

SPEC = importlib.util.spec_from_file_location('exchange_collector', Path(__file__).resolve().parents[1] / 'app' / 'services' / 'etf_exchange.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
DAY = date(2026, 11, 6)
FUNDS = {'510300.XSHG': '沪深300沪', '159919.XSHE': '沪深300深'}


def client(handler):
    return m.ExchangeClient(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)


def sse(rows, page=1, pages=1, total=None):
    return {'result': rows, 'pageHelp': {'pageNo': page, 'pageCount': pages, 'total': len(rows) if total is None else total}}


def szse(rows, page=1, pages=1, total=None):
    return [{'metadata': {'tabkey': 'tab1', 'pageno': page, 'pagecount': pages, 'recordcount': len(rows) if total is None else total}, 'data': rows, 'error': None}]


def row(code='510300', day='2026-11-06', shares='1,234.56'):
    return {'SEC_CODE': code, 'STAT_DATE': day, 'TOT_VOL': shares}


def fake_calendar():
    dates = [date(2025, 12, 31)]
    day = date(2026, 1, 1)
    official = {}
    while day <= date(2026, 11, 30):
        official[day] = trading_day(day)
        if day <= DAY and trading_day(day):
            dates.append(day)
        day += timedelta(days=1)
    return m.ExchangeCalendar(dates, DAY, 163000, now=datetime(2026, 11, 7, 10), official_days=official)


class FakeSource:
    def calendar(self):
        return fake_calendar()

    def sse_shares(self, day):
        return [row(day=day.isoformat())]

    def szse_shares(self, code, start, end):
        return [{'fund_code': code[:6]+'  ', 'size_date': end.isoformat(), 'current_size': '1234.56'}]

    def prices(self, code):
        return [{'fund_code': code, 'trade_date': DAY, 'close_price': Decimal('4.80'), 'source': 'TEST'}]


def test_conversion_and_verified_calendar():
    assert m.normalize_shares('1,405,461.66') == 14054616600
    assert m.normalize_shares('0.0001') == 1
    calendar = fake_calendar()
    assert calendar.previous(date(2026, 10, 8)) == date(2026, 9, 30)
    assert calendar.previous(date(2026, 11, 2)) == date(2026, 10, 30)
    assert calendar.previous(date(2026, 1, 5)) == date(2025, 12, 31)
    with pytest.raises(m.SourceError, match='coverage'):
        calendar.previous(date(2025, 12, 31))
    with pytest.raises(m.SourceError, match='coverage'):
        calendar.range(date(2025, 12, 30), DAY)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0.00001', '99999999999999999', None, 'bad'])
def test_bad_shares(value):
    with pytest.raises(m.SourceError):
        m.normalize_shares(value)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0', '1.000000001', '1000000000000'])
def test_bad_close(value):
    with pytest.raises(m.SourceError):
        m.normalize_close(value)


def test_sse_pagination_and_parameters():
    calls = []
    def handler(req):
        calls.append(req)
        page = int(req.url.params['pageHelp.pageNo'])
        assert req.url.params['isPagination'] == 'true'
        return httpx.Response(200, json=sse([row(code=str(510299+page))], page=page, pages=2, total=2))
    assert len(client(handler).sse_shares(DAY)) == 2
    assert len(calls) == 2


def test_szse_pagination():
    def handler(req):
        page = int(req.url.params['PAGENO'])
        assert req.url.params['jjlb'] == 'ETF'
        assert req.url.params['txtDm'] == '159919'
        return httpx.Response(200, json=szse([{'fund_code': '159919', 'size_date': f'2026-11-0{page}', 'current_size': '1'}], page=page, pages=2, total=2))
    assert len(client(handler).szse_shares('159919.XSHE', date(2026, 11, 1), DAY)) == 2


def test_page_total_change_or_truncation_rejected():
    def handler(req):
        return httpx.Response(200, json=sse([row()], total=2))
    with pytest.raises(m.SourceError, match='truncated'):
        client(handler).sse_shares(DAY)
    def changed(req):
        page = int(req.url.params['PAGENO'])
        return httpx.Response(200, json=szse([{}], page=page, pages=2, total=2 if page == 1 else 3))
    with pytest.raises(m.SourceError, match='changed during'):
        client(changed).szse_shares('159919.XSHE', DAY, DAY)


def test_retries_are_bounded_and_sanitized():
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(503, text='password=secret')
    with pytest.raises(m.SourceError, match='bounded retries') as exc:
        client(handler).sse_shares(DAY)
    assert len(calls) == 3
    assert 'secret' not in str(exc.value)


def test_collect_units_names_previous_day_and_gaps():
    shares, prices, gaps = m.collect(FakeSource(), DAY, DAY, FUNDS)
    assert len(shares) == len(prices) == 2
    assert all(r['shares'] == 12345600 for r in shares)
    assert all(r['previous_trade_date'] == date(2026, 11, 5) for r in shares)
    assert gaps == {'shares': [], 'prices': []}
    source = FakeSource()
    source.sse_shares = lambda _: []
    source.prices = lambda _: []
    shares, prices, gaps = m.collect(source, DAY, DAY, FUNDS)
    assert len(shares) == 1
    assert len(gaps['shares']) == 1 and len(gaps['prices']) == 2


@pytest.mark.parametrize('rows', [[row(), row()], [row(day='2026-11-05')], [row(day='2026-11-07')], [{'SEC_CODE': '510300'}]])
def test_bad_share_rows_abort(rows):
    source = FakeSource()
    source.sse_shares = lambda _: rows
    with pytest.raises(m.SourceError):
        m.collect(source, DAY, DAY, FUNDS)


def test_daily_price_decoders():
    def handler(req):
        if req.url.host.startswith('yunhq'):
            assert req.url.params['select'] == 'date,close'
            assert req.url.params['begin'] == '0'
            return httpx.Response(200, json={'code': '510300', 'kline': [[20261106, 4.8]]})
        if req.url.path.endswith('getTimeData'):
            return httpx.Response(200, json={'code': '0', 'datetime': '2026-11-06 15:30', 'data': {'code': '159919', 'now': '4.8', 'close': '4.7'}})
        return httpx.Response(200, json={'code': '0', 'data': {'code': '159919', 'picupdata': [['2026-11-06', '4.7', '4.8', '4.6']]}})
    source = client(handler)
    for code in FUNDS:
        rows = source.prices(code)
        assert rows[0]['trade_date'] == DAY
        assert rows[0]['close_price'] == Decimal('4.8')


@pytest.mark.parametrize('payload', [
    {'code': '510301', 'kline': [[20261106, 4.8]]},
    {'code': '510300', 'kline': [[20261106, 4.8], [20261106, 4.9]]},
    {'code': '510300', 'kline': [[20261306, 4.8]]},
    {'code': '510300', 'kline': [[20261106, 0]]},
])
def test_invalid_prices_rejected(payload):
    with pytest.raises(m.SourceError):
        client(lambda req: httpx.Response(200, json=payload)).prices('510300.XSHG')


def test_idempotency_and_same_day_revision():
    engine = create_engine('sqlite://')
    shares, prices, _ = m.collect(FakeSource(), DAY, DAY, FUNDS)
    assert m.write_records(engine, shares, prices) == 4
    assert m.write_records(engine, shares, prices) == 0
    shares[0]['shares'] += 100
    prices[0]['close_price'] = Decimal('4.9')
    assert m.write_records(engine, shares, prices) == 2
    with Session(engine) as session:
        assert len(session.scalars(select(ETFShare)).all()) == 2
        assert len(session.scalars(select(ETFPrice)).all()) == 2
        assert session.get(ETFShare, ('510300.XSHG', DAY)).shares == 12345700
        assert session.get(ETFPrice, ('510300.XSHG', DAY)).close_price == Decimal('4.9')


def test_prices_only_does_not_depend_on_share_fetch():
    source = FakeSource()
    source.sse_shares = lambda _: pytest.fail('shares should not be called')
    shares, prices, missing = m.collect(source, DAY, DAY, {'510300.XSHG': '沪'}, include_shares=False)
    assert shares == [] and len(prices) == 1 and not any(missing.values())


def test_cli_default_never_connects_database(monkeypatch):
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    monkeypatch.setattr(m.httpx, "Client", lambda **kw: http)
    monkeypatch.setattr(m.ExchangeClient, "calendar", lambda self: fake_calendar())
    shares, prices, missing = m.collect(FakeSource(), DAY, DAY, FUNDS)
    monkeypatch.setattr(m, 'collect', lambda *a, **kw: (shares, prices, missing))
    monkeypatch.setattr(m, 'write_records', lambda *a: pytest.fail('dry run must not write'))
    assert m.main(['--start', '2026-11-06', '--end', '2026-11-06']) == 0


def test_cli_incomplete_data_nonzero(monkeypatch):
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    monkeypatch.setattr(m.httpx, "Client", lambda **kw: http)
    monkeypatch.setattr(m.ExchangeClient, "calendar", lambda self: fake_calendar())
    monkeypatch.setattr(m, 'collect', lambda *a, **kw: ([], [], {'shares': [('510300.XSHG', DAY)], 'prices': []}))
    assert m.main(['--start', '2026-11-06', '--end', '2026-11-06']) == 2


@pytest.mark.parametrize('timestamp,now,count,error', [
    ('2026-11-06 14:59', '4.8', 0, False),
    ('2026-11-06 15:30', '4.8', 1, False),
    ('2026-11-06 15:30', '4.9', 0, True),
    ('2026-11-09 15:30', '4.8', 0, False),
])
def test_szse_rejects_unverified_history_and_intraday(timestamp, now, count, error):
    def handler(req):
        if req.url.path.endswith('getTimeData'):
            return httpx.Response(200, json={'code': '0', 'datetime': timestamp, 'data': {'code': '159919', 'now': now, 'close': '999'}})
        return httpx.Response(200, json={'code': '0', 'data': {'code': '159919', 'picupdata': [['2026-11-05', 1, 4.7], ['2026-11-06', 1, 4.8]]}})
    if error:
        with pytest.raises(m.SourceError, match='disagrees'):
            client(handler).prices('159919.XSHE')
    else:
        rows = client(handler).prices('159919.XSHE')
        assert len(rows) == count
        if rows:
            assert rows[0]['trade_date'] == DAY and rows[0]['source'] == 'SZSE.CLOSE_VERIFIED'


def test_write_failure_rolls_back_both_tables():
    from sqlalchemy.exc import IntegrityError
    engine = create_engine('sqlite://')
    shares, prices, _ = m.collect(FakeSource(), DAY, DAY, FUNDS)
    m.write_records(engine, shares, prices)
    shares[0]['shares'] = 999
    prices[0]['close_price'] = None
    with pytest.raises(IntegrityError):
        m.write_records(engine, shares, prices)
    with Session(engine) as session:
        assert session.get(ETFShare, ('510300.XSHG', DAY)).shares == 12345600
        assert session.get(ETFPrice, ('510300.XSHG', DAY)).close_price == Decimal('4.8')


def test_sse_current_intraday_bar_not_saved(monkeypatch):
    from datetime import datetime
    class Intraday(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 6, 14, 59, tzinfo=tz)
    monkeypatch.setattr(m, 'datetime', Intraday)
    source = client(lambda req: httpx.Response(200, json={'code': '510300', 'kline': [[20261105, 4.7], [20261106, 4.8], [20261109, 4.9]]}))
    assert [r['trade_date'] for r in source.prices('510300.XSHG')] == [date(2026, 11, 5)]


def test_identical_recheck_preserves_timestamp_revision_updates_it():
    from datetime import datetime
    engine = create_engine('sqlite://')
    shares, prices, _ = m.collect(FakeSource(), DAY, DAY, FUNDS)
    first = datetime(2026, 11, 6, 16)
    later = datetime(2026, 11, 7, 8)
    for record in shares + prices:
        record['updated_at'] = first
    m.write_records(engine, shares, prices)
    for record in shares + prices:
        record['updated_at'] = later
    m.write_records(engine, shares, prices)
    with Session(engine) as session:
        assert session.get(ETFShare, ('510300.XSHG', DAY)).updated_at == first
        assert session.get(ETFPrice, ('510300.XSHG', DAY)).updated_at == first
    shares[0]['shares'] += 1
    prices[0]['close_price'] += Decimal('.01')
    m.write_records(engine, shares, prices)
    with Session(engine) as session:
        assert session.get(ETFShare, ('510300.XSHG', DAY)).updated_at == later
        assert session.get(ETFPrice, ('510300.XSHG', DAY)).updated_at == later


def test_future_end_rejected_before_network():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    future = datetime.now(ZoneInfo('Asia/Shanghai')).date() + timedelta(days=1)
    with pytest.raises(m.SourceError, match='future'):
        m.collect(None, future, future, FUNDS)


def month_payload(month, today=date(2026, 11, 7)):
    import calendar
    return {'nowdate': today.isoformat(), 'data': [
        {'jyrq': date(month.year, month.month, n).isoformat(), 'jybz': '1' if trading_day(date(month.year, month.month, n)) else '0'}
        for n in range(1, calendar.monthrange(month.year, month.month)[1] + 1)]}


def test_official_month_calendar_validates_every_day():
    payload = month_payload(date(2026, 10, 1))
    days = client(lambda req: httpx.Response(200, json=payload)).month_calendar(date(2026, 10, 1), date(2026, 11, 7))
    assert len(days) == 31
    assert not days[date(2026, 10, 1)]
    assert days[date(2026, 10, 8)]
    assert not days[date(2026, 10, 10)]  # Civil make-up Saturday is still exchange-closed.


@pytest.mark.parametrize('corruption', ['stale', 'empty', 'missing', 'duplicate', 'outside', 'marker'])
def test_bad_month_calendar_fails_closed(corruption):
    payload = month_payload(date(2026, 11, 1))
    if corruption == 'stale':
        payload['nowdate'] = '2026-11-06'
    elif corruption == 'empty':
        payload['data'] = []
    elif corruption == 'missing':
        payload['data'].pop()
    elif corruption == 'duplicate':
        payload['data'][-1] = payload['data'][0]
    elif corruption == 'outside':
        payload['data'][0]['jyrq'] = '2026-10-31'
    else:
        payload['data'][0]['jybz'] = 'unknown'
    with pytest.raises(m.SourceError):
        client(lambda req: httpx.Response(200, json=payload)).month_calendar(date(2026, 11, 1), date(2026, 11, 7))


def test_calendar_default_uses_completed_sessions_and_official_closures():
    calendar = fake_calendar()
    assert calendar.default_range(5) == (date(2026, 11, 2), DAY)
    with pytest.raises(m.SourceError, match='coverage'):
        calendar.default_range(0)
    with pytest.raises(m.SourceError, match='coverage'):
        calendar.range(DAY, date(2026, 11, 7))


def test_calendar_stale_bars_not_treated_as_closure():
    good = fake_calendar()
    stale = m.ExchangeCalendar(list(good.days[:-1]), date(2026, 11, 5), 163000,
                               now=good.now, official_days=good.official_days)
    with pytest.raises(m.SourceError, match='stale'):
        stale.default_range()


def test_calendar_detects_missing_interior_session():
    good = fake_calendar()
    incomplete = [d for d in good.days if d != date(2026, 11, 4)]
    with pytest.raises(m.SourceError, match='disagrees'):
        m.ExchangeCalendar(incomplete, DAY, 163000, now=good.now, official_days=good.official_days)


def test_calendar_unknown_month_does_not_infer_weekday():
    cal = m.ExchangeCalendar([date(2026, 12, 30), date(2026, 12, 31)], date(2026, 12, 31), 163000,
                             now=datetime(2027, 1, 4, 18))
    with pytest.raises(m.SourceError, match='unavailable'):
        cal.default_range(1)


def test_calendar_new_year_accepts_published_official_dates():
    dates = [date(2026, 12, 31), date(2027, 1, 4)]
    official = {date(2027, 1, n): n == 4 for n in range(1, 5)}
    official[date(2026, 12, 31)] = True
    cal = m.ExchangeCalendar(dates, dates[-1], 163000, now=datetime(2027, 1, 4, 18), official_days=official)
    assert cal.default_range(1) == (dates[-1], dates[-1])
    assert cal.previous(dates[-1]) == date(2026, 12, 31)


def test_calendar_intraday_excluded_even_with_current_day_index_bar():
    dates = [date(2026, 11, 5), DAY]
    cal = m.ExchangeCalendar(dates, DAY, 143000, now=datetime(2026, 11, 6, 14, 30),
                             official_days={d: True for d in dates})
    assert cal.default_range(1) == (date(2026, 11, 5), date(2026, 11, 5))
    with pytest.raises(m.SourceError, match='coverage'):
        cal.range(DAY, DAY)


@pytest.mark.parametrize('corruption', ['code', 'total', 'begin', 'end', 'first', 'duplicate', 'order', 'snapshot_date'])
def test_calendar_http_full_history_guards(corruption, monkeypatch):
    # Keep transport fully offline while exercising schema and completeness checks.
    good = fake_calendar()
    dates = [date(1990, 12, 19)] + list(good.days)
    payload = {'code': '000001', 'begin': 0, 'end': len(dates), 'total': len(dates), 'kline': [[int(d.strftime('%Y%m%d'))] for d in dates]}
    snap = {'code': '000001', 'date': 20261106, 'snap': [20261106, 163000]}
    if corruption in ('code', 'total', 'begin', 'end'):
        payload[corruption] = '999999' if corruption == 'code' else 1
    elif corruption == 'first':
        payload['kline'][0] = [19901220]
    elif corruption == 'duplicate':
        payload['kline'][-2] = payload['kline'][-1]
    elif corruption == 'order':
        payload['kline'][-2], payload['kline'][-1] = payload['kline'][-1], payload['kline'][-2]
    else:
        snap['snap'][0] = 20261105
    source = client(lambda req: httpx.Response(200, json=snap if '/snap/' in req.url.path else payload))
    monkeypatch.setattr(source, 'month_calendar', lambda *a: good.official_days)
    with pytest.raises(m.SourceError):
        source.calendar()


def test_calendar_saved_idempotently_in_same_transaction():
    engine = create_engine('sqlite://')
    cal = fake_calendar()
    shares, prices, _ = m.collect(FakeSource(), DAY, DAY, FUNDS, calendar=cal)
    m.write_records(engine, shares, prices, calendar=cal)
    m.write_records(engine, shares, prices, calendar=cal)
    with Session(engine) as session:
        assert len(session.scalars(select(ETFTradingDay)).all()) == len(cal.days)
        assert session.get(ETFTradingDay, date(2026, 1, 5)).previous_trade_date == date(2025, 12, 31)
        assert session.get(ETFTradingDay, cal.days[0]).previous_trade_date is None


def test_calendar_and_data_write_rollback_together():
    from sqlalchemy.exc import IntegrityError
    engine = create_engine('sqlite://')
    cal = fake_calendar()
    shares, prices, _ = m.collect(FakeSource(), DAY, DAY, FUNDS, calendar=cal)
    prices[0]['close_price'] = None
    with pytest.raises(IntegrityError):
        m.write_records(engine, shares, prices, calendar=cal)
    with Session(engine) as session:
        assert not session.scalars(select(ETFTradingDay)).all()
        assert not session.scalars(select(ETFShare)).all()


def test_cli_no_dates_uses_dynamic_calendar_default(monkeypatch):
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    monkeypatch.setattr(m.httpx, 'Client', lambda **kw: http)
    monkeypatch.setattr(m.ExchangeClient, 'calendar', lambda self: fake_calendar())
    called = []
    def collect(*args, **kwargs):
        called.append((args[1], args[2]))
        assert kwargs['calendar'].days[-1] == DAY
        return [], [], {'shares': [], 'prices': []}
    monkeypatch.setattr(m, 'collect', collect)
    monkeypatch.setattr(m, 'write_records', lambda *a, **kw: pytest.fail('default must not write'))
    assert m.main([]) == 0
    assert called == [(date(2026, 11, 2), DAY)]


@pytest.mark.parametrize('hour,minute', [(9, 30), (11, 30)])
def test_intraday_today_snapshot_with_completed_only_dayk(hour, minute):
    dates = [date(2026, 11, 4), date(2026, 11, 5)]
    official = {date(2026, 11, n): True for n in (4, 5, 6)}
    cal = m.ExchangeCalendar(dates, DAY, hour * 10000 + minute * 100,
                             now=datetime(2026, 11, 6, hour, minute), official_days=official)
    cal.require_fresh()
    assert cal.default_range(1) == (date(2026, 11, 5), date(2026, 11, 5))
    assert DAY not in cal.days


def test_intraday_today_snapshot_does_not_accept_older_missing_session():
    official = {date(2026, 11, n): True for n in (3, 4, 5, 6)}
    with pytest.raises(m.SourceError, match='disagrees'):
        m.ExchangeCalendar([date(2026, 11, 3), date(2026, 11, 4)], DAY, 113000,
                           now=datetime(2026, 11, 6, 11, 30), official_days=official)


@pytest.mark.parametrize('hour,minute', [(15, 0), (18, 30)])
def test_post_close_today_snapshot_with_yesterday_dayk_still_fails(hour, minute):
    official = {date(2026, 11, n): True for n in (4, 5, 6)}
    with pytest.raises(m.SourceError, match='disagrees'):
        m.ExchangeCalendar([date(2026, 11, 4), date(2026, 11, 5)], DAY, hour * 10000 + minute * 100,
                           now=datetime(2026, 11, 6, hour, minute), official_days=official)


def test_intraday_completed_only_dayk_resolves_holiday_gap():
    # The first day after National Day must point to Sep 30, not yesterday.
    official = {date(2026, 9, 30): True, **{date(2026, 10, n): False for n in range(1, 8)}, date(2026, 10, 8): True}
    cal = m.ExchangeCalendar([date(2026, 9, 29), date(2026, 9, 30)], date(2026, 10, 8), 113000,
                             now=datetime(2026, 10, 8, 11, 30), official_days=official)
    assert cal.default_range(1) == (date(2026, 9, 30), date(2026, 9, 30))
