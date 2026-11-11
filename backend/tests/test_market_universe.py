"""Public exchange contracts; all requests and securities are synthetic/offline."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import gzip
import json
import re
from zipfile import ZipFile

import httpx
from openpyxl import Workbook
import pytest

from app.services.market_diagnostics import source_failure_details
from app.services.market_universe import (
    ExchangeSecurityMaster, SSE_DELIST_URL, SSE_MASTER_URL, SZSE_METADATA_URL, UniverseDataError,
)


NOW = datetime(2026, 11, 11, 1, tzinfo=timezone.utc)


def sse(code, board='1', start='20000101', end='-', name='测试证券'):
    return {'A_STOCK_CODE': code, 'STOCK_TYPE': board, 'LIST_DATE': start,
            'DELIST_DATE': end, 'SEC_NAME_CN': name, 'COMPANY_ABBR': name}


def workbook(headers, rows, *, stale_dimensions=False):
    book = Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    book.save(stream)
    book.close()
    result = stream.getvalue()
    if stale_dimensions:
        out = BytesIO()
        with ZipFile(BytesIO(result)) as source, ZipFile(out, 'w') as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == 'xl/worksheets/sheet1.xml':
                    data = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', data)
                target.writestr(item, data)
        result = out.getvalue()
    return result


class ExchangeFixture:
    def __init__(self):
        self.sse_current = [sse('600001'), sse('689009', '8', '20201029'),
                            sse('688999', '8', '20261110')]
        self.sse_delisted = [sse('600002', end='20261107')]
        self.sz = {
            ('1110', 'tab1'): (['A股代码', 'A股简称', 'A股上市日期'],
                              [['000001', '深证一', '1991-04-03']]),
            ('1110', 'tab3'): (['CDR代码', 'CDR简称', '上市日期'], []),
            ('1793_ssgs', 'tab2'): (['证券代码', '证券简称', '上市日期', '终止上市日期'],
                                    [['000002', '深证二', '1991-01-01', '2026-11-06'],
                                     ['200001', '测试B股', '1992-01-01', '2026-11-07']]),
            ('1793_ssgs', 'tab1'): (['证券代码', '证券简称', '上市日期', '暂停上市日期'], []),
        }
        self.calls = []
        self.meta_calls = {}
        self.count_change = False
        self.stale_dimensions = False
        self.truncate_xlsx = False
        self.omit_tab = False

    def serve(self, request):
        self.calls.append(request)
        query = request.url.params
        if request.url.host == 'query.sse.com.cn':
            rows = self.sse_delisted if query['COMPANY_STATUS'] == '3' else self.sse_current
            assert query['STOCK_TYPE'] == '1,8'
            assert request.headers['referer'].startswith('https://www.sse.com.cn/')
            assert query['isPagination'] == 'true'
            assert query['pageHelp.cacheSize'] == '1'
            assert query['pageHelp.beginPage'] == query['pageHelp.endPage'] == query['pageHelp.pageNo']
            size, page = int(query['pageHelp.pageSize']), int(query['pageHelp.pageNo'])
            assert size == 20
            return httpx.Response(200, json={
                'result': rows[(page - 1) * size:page * size], 'pageHelp': {'total': len(rows)}})
        key = query['CATALOGID'], query['TABKEY']
        headers, rows = self.sz[key]
        if str(request.url).startswith(SZSE_METADATA_URL):
            self.meta_calls[key] = self.meta_calls.get(key, 0) + 1
            count = len(rows)
            if self.count_change and self.meta_calls[key] == 2:
                count += 1
            size = 20
            metadata = {'catalogid': key[0], 'tabkey': key[1], 'recordcount': count,
                        'pagesize': size, 'pageno': 1, 'pagecount': (count + size - 1) // size,
                        'cols': {str(index): name for index, name in enumerate(headers)}}
            data = [{} for _ in range(min(count, size))]
            return httpx.Response(200, json=[] if self.omit_tab else [{'metadata': metadata, 'data': data}])
        return httpx.Response(200, content=workbook(headers, rows[:-1] if self.truncate_xlsx else rows,
                                                  stale_dimensions=self.stale_dimensions))


def fetch(fixture, day='2026-11-06'):
    with httpx.Client(transport=httpx.MockTransport(fixture.serve)) as client:
        with ExchangeSecurityMaster(client, now=lambda: NOW) as master:
            result = master.fetch(day)
        assert not client.is_closed
        return result


def test_dynamic_membership_cdr_ipo_delist_and_no_bse_or_bshares():
    fixture = ExchangeFixture()
    result = fetch(fixture)
    assert {row['symbol'] for row in result['rows']} == {'sh600001', 'sh689009', 'sh600002', 'sz000001'}
    cdr = next(row for row in result['rows'] if row['symbol'] == 'sh689009')
    assert cdr['type'] == cdr['security_type'] == 'cdr'
    assert cdr['listed_from'] == '2020-10-29'
    assert cdr['listed_until'] is None
    assert result['expected_count'] == len(result['rows'])
    assert result['evidence']['source_counts_verified'] is True
    assert result['evidence']['universe_verified'] is False
    assert result['evidence']['historical_membership_verified'] is False
    assert result['evidence']['coverage_from'] is None
    assert result['limitations']
    json.dumps(result)


def test_listing_day_inclusive_delisting_day_exclusive_not_last_bar_date():
    fixture = ExchangeFixture()
    assert 'sh600002' not in {row['symbol'] for row in fetch(fixture, '2026-11-07')['rows']}
    fixture = ExchangeFixture()
    result = fetch(fixture, '2026-11-10')
    assert 'sh688999' in {row['symbol'] for row in result['rows']}
    assert all(row['listed_until_exclusive'] for row in result['rows'])


def test_current_catalogue_count_is_verified_and_not_a_historical_coverage_claim():
    result = fetch(ExchangeFixture(), '2026-11-11')
    assert result['evidence']['universe_verified'] is True
    assert result['evidence']['historical_membership_verified'] is False
    assert result['source_has_trading_date'] is False


def test_stale_official_xlsx_dimensions_do_not_drop_records():
    fixture = ExchangeFixture()
    fixture.stale_dimensions = True
    assert fetch(fixture)['expected_count'] == 4


def test_empty_cdr_is_explicit_valid_metadata_not_silently_ignored_endpoint_failure():
    fixture = ExchangeFixture()
    fetch(fixture)
    assert fixture.meta_calls[('1110', 'tab3')] == 2
    assert not any(request.url.params.get('SHOWTYPE') and request.url.params['TABKEY'] == 'tab3'
                   for request in fixture.calls)


def test_future_cdr_is_discovered_without_new_security_whitelist():
    fixture = ExchangeFixture()
    fixture.sse_current.append(sse('689123', '8', '20260101'))
    fixture.sz[('1110', 'tab3')][1].append(['302123', '测试存托凭证', '2026-01-02'])
    result = fetch(fixture)
    assert {'sh689123', 'sz302123'}.issubset({row['symbol'] for row in result['rows']})
    assert next(row for row in result['rows'] if row['symbol'] == 'sz302123')['type'] == 'cdr'


@pytest.mark.parametrize('flag,code', [('count_change', 'count_changed'),
                                      ('truncate_xlsx', 'count_changed'),
                                      ('omit_tab', 'invalid_page')])
def test_incomplete_or_changing_source_fails_closed(flag, code):
    fixture = ExchangeFixture()
    setattr(fixture, flag, True)
    with pytest.raises(UniverseDataError) as error:
        fetch(fixture)
    assert error.value.code == code


@pytest.mark.parametrize('mutation,code', [
    (lambda f: f.sse_current.append(deepcopy(f.sse_current[0])), 'duplicate_symbol'),
    (lambda f: f.sse_current[0].update(LIST_DATE='bad'), 'invalid_date'),
    (lambda f: f.sse_delisted[0].update(DELIST_DATE='-'), 'invalid_interval'),
    (lambda f: f.sse_delisted[0].update(A_STOCK_CODE='600001'), 'ambiguous_listing_intervals'),
])
def test_invalid_listing_evidence_is_never_dropped_or_coerced(mutation, code):
    fixture = ExchangeFixture()
    mutation(fixture)
    with pytest.raises(UniverseDataError) as error:
        fetch(fixture)
    assert error.value.code == code


def test_disjoint_relisting_intervals_preserve_historical_old_listing():
    fixture = ExchangeFixture()
    fixture.sse_current[0]['LIST_DATE'] = '20261110'
    fixture.sse_delisted[0]['A_STOCK_CODE'] = '600001'
    old = next(row for row in fetch(fixture)['rows'] if row['symbol'] == 'sh600001')
    assert old['delisted_on'] == '2026-11-07'


@pytest.mark.parametrize('status', [403, 429])
def test_denial_stops_without_retries_or_fallbacks(status):
    calls = []
    def denied(request):
        calls.append(request)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(denied)) as client:
        with pytest.raises(UniverseDataError) as error:
            ExchangeSecurityMaster(client, now=lambda: NOW).fetch('2026-11-06')
    assert len(calls) == 1
    assert error.value.is_rate_limited
    assert error.value.status_code == status


@pytest.mark.parametrize('day', ['2026-11-12', datetime(2026, 11, 6), '20261106', '2026-02-30'])
def test_future_or_invalid_requested_date_never_reaches_network(day):
    fixture = ExchangeFixture()
    with pytest.raises(UniverseDataError):
        fetch(fixture, day)
    assert not fixture.calls


def paged_fixture(count=43):
    fixture = ExchangeFixture()
    fixture.sse_current = [sse(str(600100 + index), '8' if index == count - 1 else '1')
                           for index in range(count)]
    return fixture


@pytest.mark.parametrize('count,expected_pages', [(20, 1), (40, 2), (43, 3)])
def test_sse_pages_are_sequential_bounded_and_last_page_is_exact(count, expected_pages):
    fixture = paged_fixture(count)
    result = fetch(fixture)
    current = next(source for source in result['raw']['sources'] if source['source_id'] == 'sse_current')
    assert current['record_count'] == len(current['raw_rows']) == count
    assert current['pages'] == expected_pages
    requests = [request for request in fixture.calls
                if request.url.host == 'query.sse.com.cn' and request.url.params['COMPANY_STATUS'] != '3']
    assert [request.url.params['pageHelp.pageNo'] for request in requests] == [
        str(page) for page in range(1, expected_pages + 1)]
    assert all(str(request.url).split('?')[0] == SSE_MASTER_URL for request in requests)
    assert len({row['symbol'] for row in result['rows']}) == result['expected_count']


@pytest.mark.parametrize('count', [0, 23])
def test_sse_delisted_pages_include_explicit_empty_catalogue(count):
    fixture = ExchangeFixture()
    fixture.sse_delisted = [sse(str(600100 + index), end='20261107') for index in range(count)]
    result = fetch(fixture)
    terminated = next(source for source in result['raw']['sources'] if source['source_id'] == 'sse_delisted')
    assert terminated['record_count'] == len(terminated['raw_rows']) == count
    assert terminated['pages'] == max(1, (count + 19) // 20)
    calls = [request for request in fixture.calls if request.url.params.get('COMPANY_STATUS') == '3']
    assert len(calls) == terminated['pages']
    assert all(str(request.url).split('?')[0] == SSE_DELIST_URL for request in calls)


@pytest.mark.parametrize('rows,code', [([], 'invalid_count'), ([sse('600001')], 'missing_market')])
def test_current_sse_requires_nonempty_catalogue_and_both_boards(rows, code):
    fixture = ExchangeFixture()
    fixture.sse_current = rows
    with pytest.raises(UniverseDataError) as error:
        fetch(fixture)
    assert error.value.code == code
    assert len(fixture.calls) == 1


def test_optional_sse_metadata_and_mirrored_data_are_verified_not_concatenated():
    fixture = paged_fixture()
    def serve(request):
        response = fixture.serve(request)
        if request.url.host == 'query.sse.com.cn':
            payload = response.json()
            query = request.url.params
            total = payload['pageHelp']['total']
            payload.update(pageNo=None, pageSize=None, actionErrors=[], fieldErrors={})
            payload['pageHelp'].update(pageNo=int(query['pageHelp.pageNo']), pageSize=20,
                                       pageCount=(total + 19) // 20, data=deepcopy(payload['result']))
            return httpx.Response(200, json=payload)
        return response
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = ExchangeSecurityMaster(client, now=lambda: NOW).fetch('2026-11-06')
    assert result['expected_count'] == 45  # 43 current SSE + one delisted SSE + one SZSE
    assert len(result['raw']['sources'][0]['raw_rows']) == 43


@pytest.mark.parametrize('mutation,code', [
    (lambda payload: payload['pageHelp'].update(pageNo=2), 'invalid_page'),
    (lambda payload: payload['pageHelp'].update(pageSize=100), 'invalid_page'),
    (lambda payload: payload['pageHelp'].update(pageCount=99), 'invalid_page'),
    (lambda payload: payload['pageHelp'].update(data=[]), 'invalid_page'),
    (lambda payload: payload.update(actionErrors=['query failed']), 'invalid_page'),
    (lambda payload: payload.update(fieldErrors={'sqlId': ['invalid']}), 'invalid_page'),
    (lambda payload: payload['result'].pop(), 'incomplete_page'),
    (lambda payload: payload['result'].append(sse('600999')), 'incomplete_page'),
    (lambda payload: payload.update(result=[sse(str(600100 + i)) for i in range(43)]), 'incomplete_page'),
    (lambda payload: payload['result'][0].update(LIST_DATE='bad'), 'invalid_date'),
    (lambda payload: payload['result'][0].update(STOCK_TYPE='2'), 'invalid_type'),
    (lambda payload: payload['result'].__setitem__(0, None), 'invalid_type'),
    (lambda payload: payload['result'][1].update(A_STOCK_CODE='600100'), 'duplicate_symbol'),
])
def test_bad_sse_first_page_fails_before_requesting_more_pages(mutation, code):
    fixture = paged_fixture()
    def serve(request):
        response = fixture.serve(request)
        payload = response.json()
        mutation(payload)
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert master._catalogue is None
    assert error.value.code == code
    assert len(fixture.calls) == 1


@pytest.mark.parametrize('change,code', [('repeat', 'duplicate_symbol'), ('count', 'count_changed'),
                                        ('truncated', 'incomplete_page')])
def test_later_sse_page_failure_discards_partial_catalogue(change, code):
    fixture = paged_fixture()
    def serve(request):
        response = fixture.serve(request)
        if request.url.params.get('pageHelp.pageNo') == '2':
            payload = response.json()
            if change == 'repeat':
                payload['result'] = fixture.sse_current[:20]
            elif change == 'count':
                payload['pageHelp']['total'] += 1
            else:
                payload['result'].pop()
            return httpx.Response(200, json=payload)
        return response
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert master._catalogue is None
    assert error.value.code == code
    assert len(fixture.calls) == 2


@pytest.mark.parametrize('status', [302, 403, 429])
def test_later_page_denial_or_redirect_stops_and_never_reuses_partial_pages(status):
    fixture = paged_fixture()
    deny = True
    def serve(request):
        response = fixture.serve(request)
        if deny and request.url.params.get('pageHelp.pageNo') == '2':
            return httpx.Response(status, headers={'Location': 'https://www.sse.com.cn/other'})
        return response
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.status_code == status
        assert len(fixture.calls) == 2
        assert master._catalogue is None
        # An explicit later caller request starts from page 1, not a partial
        # catalogue. The failed fetch itself never retries or switches hosts.
        deny = False
        result = master.fetch('2026-11-07')
    assert fixture.calls[2].url.params['pageHelp.pageNo'] == '1'
    assert result['expected_count'] == 44


def test_cache_reuses_complete_catalogue_but_filters_each_date_and_preserves_provenance():
    fixture = paged_fixture()
    fixture.sse_current[-1]['LIST_DATE'] = '20261110'
    clock = [NOW - timedelta(hours=2)]  # UTC date changes, Shanghai date does not.
    with httpx.Client(transport=httpx.MockTransport(fixture.serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: clock[0])
        old = master.fetch('2026-11-06')
        original_raw = deepcopy(old['raw'])
        calls = len(fixture.calls)
        clock[0] = NOW + timedelta(hours=1)
        later = master.fetch('2026-11-10')
        today = master.fetch('2026-11-11')
    assert len(fixture.calls) == calls
    assert len(original_raw['sources']) == 6
    assert all(value == 2 for value in fixture.meta_calls.values())
    assert 'sh600002' in {row['symbol'] for row in old['rows']}
    assert 'sh600002' not in {row['symbol'] for row in later['rows']}
    assert 'sh600142' not in {row['symbol'] for row in old['rows']}
    assert 'sh600142' in {row['symbol'] for row in later['rows']}
    for result in (old, later, today):
        assert result['fetched_at'] == (NOW - timedelta(hours=2)).isoformat()
        assert result['evidence']['snapshot_date'] == '2026-11-11'
        assert result['expected_count'] == len(result['rows'])
        assert result['raw'] == original_raw
        assert result['evidence']['historical_membership_verified'] is False
    assert later['evidence']['membership_verified'] is False
    assert later['limitations']
    assert today['evidence']['membership_verified'] is True
    assert today['limitations'] == []


def test_caller_mutations_cannot_poison_cached_membership_or_raw_evidence():
    fixture = ExchangeFixture()
    with httpx.Client(transport=httpx.MockTransport(fixture.serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        result = master.fetch('2026-11-06')
        expected = deepcopy(result)
        calls = len(fixture.calls)
        result['rows'][0]['listed_from'] = '2100-01-01'
        result['catalogue_rows'][0]['evidence']['raw']['A_STOCK_CODE'] = '999999'
        result['raw']['sources'][0]['record_count'] = 0
        result['raw']['requests'][0]['params']['pageHelp.pageNo'] = '999'
        result['evidence']['source_counts_verified'] = False
        result['catalogue_rows'].clear()
        result['rows'].clear()
        assert master.fetch('2026-11-06') == expected
    assert len(fixture.calls) == calls


def test_late_source_failure_is_not_cached_and_all_sources_are_revalidated():
    fixture = ExchangeFixture()
    fixture.count_change = True
    with httpx.Client(transport=httpx.MockTransport(fixture.serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError, match='深交所名单缺失'):
            master.fetch('2026-11-06')
        assert master._catalogue is None
        calls = len(fixture.calls)
        fixture.count_change = False
        result = master.fetch('2026-11-06')
    assert fixture.calls[calls].url.params['COMPANY_STATUS'] == '2,4,5,7,8'
    assert fixture.calls[calls].url.params['pageHelp.pageNo'] == '1'
    assert len(result['raw']['sources']) == 6


def test_cache_is_instance_scoped_and_cleared_on_close():
    fixture = ExchangeFixture()
    with httpx.Client(transport=httpx.MockTransport(fixture.serve)) as client:
        first = ExchangeSecurityMaster(client, now=lambda: NOW)
        first.fetch('2026-11-06')
        calls = len(fixture.calls)
        second = ExchangeSecurityMaster(client, now=lambda: NOW)
        second.fetch('2026-11-06')
        assert len(fixture.calls) == 2 * calls
        first.close()
        assert first._catalogue is None
        first.fetch('2026-11-06')
        assert len(fixture.calls) == 3 * calls
        assert not client.is_closed


def test_shanghai_date_change_invalidates_cache_even_without_utc_date_change():
    fixture = ExchangeFixture()
    clock = [NOW.replace(hour=15, minute=59)]
    with httpx.Client(transport=httpx.MockTransport(fixture.serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: clock[0])
        old = master.fetch('2026-11-11')
        calls = len(fixture.calls)
        clock[0] = clock[0].replace(hour=16, minute=1)
        fresh = master.fetch('2026-11-11')
    assert len(fixture.calls) == 2 * calls
    assert old['evidence']['snapshot_date'] == '2026-11-11'
    assert fresh['evidence']['snapshot_date'] == '2026-11-12'
    assert fresh['evidence']['membership_verified'] is False
    assert fresh['fetched_at'] == clock[0].isoformat()


def test_failed_refresh_does_not_fall_back_to_previous_day_catalogue():
    fixture = ExchangeFixture()
    clock = [NOW]
    deny = False
    def serve(request):
        if deny:
            fixture.calls.append(request)
            return httpx.Response(403)
        return fixture.serve(request)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: clock[0])
        master.fetch('2026-11-06')
        calls = len(fixture.calls)
        clock[0] += timedelta(days=1)
        deny = True
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.status_code == 403
        assert master._catalogue is None
        assert len(fixture.calls) == calls + 1


def test_midnight_during_capture_is_rejected_without_caching_or_automatic_retry():
    fixture = ExchangeFixture()
    clock = [NOW.replace(hour=15, minute=59)]
    def serve(request):
        clock[0] = NOW.replace(hour=16, minute=1)
        return fixture.serve(request)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: clock[0])
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-11')
        assert error.value.code == 'capture_date_changed'
        assert error.value.source_diagnostics == {
            'source_id': 'exchange_master', 'stage': 'catalogue_validation',
            'code': 'capture_date_changed', 'status_code': None,
        }
        assert 'source_code=capture_date_changed' in source_failure_details(error.value)
        assert master._catalogue is None
        calls = len(fixture.calls)
        assert sum(request.url.params.get('COMPANY_STATUS') == '2,4,5,7,8'
                   for request in fixture.calls) == 1
        result = master.fetch('2026-11-12')
    assert len(fixture.calls) == 2 * calls
    assert result['evidence']['snapshot_date'] == '2026-11-12'
    assert result['fetched_at'] == clock[0].isoformat()


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.pulls, self.closed = chunks, 0, False

    def __iter__(self):
        for chunk in self.chunks:
            self.pulls += 1
            if callable(chunk):
                chunk = chunk()
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize('kwargs,expected', [
    ({}, {'connect': 30.0, 'read': 60.0, 'write': 30.0, 'pool': 30.0}),
    ({'timeout': 7.5}, {'connect': 7.5, 'read': 7.5, 'write': 7.5, 'pool': 7.5}),
    ({'timeout': httpx.Timeout(9, read=17)}, {'connect': 9, 'read': 17, 'write': 9, 'pool': 9}),
])
def test_stream_default_extends_only_read_timeout_and_preserves_custom_timeout(kwargs, expected):
    calls = []
    body = TrackedStream([b'{"ok":', b'true}'])
    def serve(request):
        calls.append(request)
        assert request.extensions['timeout'] == expected
        return httpx.Response(200, stream=body)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        response = ExchangeSecurityMaster(client, **kwargs)._request(SSE_MASTER_URL, {}, [])
        assert response.json() == {'ok': True}
        assert response.content == b'{"ok":true}'
        assert not client.is_closed
    assert body.closed and len(calls) == 1


@pytest.mark.parametrize('status', [302, 403, 429, 503])
def test_stream_rejects_status_at_headers_without_reading_or_following(status):
    body = TrackedStream([AssertionError('Error bodies must never be read.')])
    calls = []
    def serve(request):
        calls.append(request)
        return httpx.Response(status, stream=body, headers={'Location': 'https://www.sse.com.cn/other'})
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.status_code == status
        assert master._catalogue is None
        assert master._catalogue_deadline is None
    assert len(calls) == 1 and body.closed and body.pulls == 0


def test_trickling_body_hits_request_budget_closes_and_discards_partial_catalogue():
    clock, calls = [0.0], []
    def slow_chunk():
        clock[0] += 45
        return b' '
    body = TrackedStream([slow_chunk] * 8)
    def serve(request):
        calls.append(request)
        return httpx.Response(200, stream=body)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW, monotonic=lambda: clock[0])
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.code == 'request_deadline'
        assert error.value.status_code == 200
        assert 'source_code=request_deadline http_status=200' in source_failure_details(error.value)
        assert master._catalogue is None and master._catalogue_deadline is None
    assert clock[0] == 135 and body.pulls == 3 and body.closed and len(calls) == 1


def test_compressed_trickle_checks_raw_chunks_even_without_decoded_output():
    clock = [0.0]
    compressed = gzip.compress(b'{"result": []}')
    def byte_chunk(value):
        def emit():
            clock[0] += 45
            return bytes([value])
        return emit
    body = TrackedStream([byte_chunk(value) for value in compressed])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, stream=body, headers={'Content-Encoding': 'gzip'}))) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW, monotonic=lambda: clock[0])
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
    assert error.value.code == 'request_deadline'
    assert body.pulls == 3 and body.closed  # Still in the gzip header, no decoded JSON yet.


def test_catalogue_budget_spans_requests_and_stops_before_cache_publication():
    fixture, clock, streams = ExchangeFixture(), [0.0], []
    def serve(request):
        response = fixture.serve(request)
        def emit():
            clock[0] += 100
            return response.content
        body = TrackedStream([emit])
        streams.append(body)
        return httpx.Response(200, stream=body, headers=response.headers)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW, monotonic=lambda: clock[0])
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.code == 'catalogue_deadline'
        assert error.value.status_code == 200
        assert 'source_code=catalogue_deadline http_status=200' in source_failure_details(error.value)
        assert master._catalogue is None and master._catalogue_deadline is None
    assert clock[0] == 600 and len(fixture.calls) == 6
    assert all(body.closed for body in streams)


def test_catalogue_budget_before_next_request_has_no_invented_http_status():
    fixture, clock = ExchangeFixture(), [0.0]
    with httpx.Client(transport=httpx.MockTransport(fixture.serve)) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW, monotonic=lambda: clock[0])
        load = master._sse
        def expire_after_first_page(**kwargs):
            result = load(**kwargs)
            clock[0] = 601
            return result
        master._sse = expire_after_first_page
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.code == 'catalogue_deadline' and error.value.status_code is None
        assert 'source_code=catalogue_deadline http_status=-' in source_failure_details(error.value)
        assert master._catalogue is None and master._catalogue_deadline is None
        assert len(fixture.calls) == 1
        master._sse = load
        # A new explicit fetch starts a fresh budget and reloads all sources.
        assert master.fetch('2026-11-06')['expected_count'] == 4
    assert fixture.calls[1].url.params['pageHelp.pageNo'] == '1'


@pytest.mark.parametrize('compressed', [False, True])
def test_streamed_decoded_json_and_workbook_match_existing_evidence(compressed):
    fixture, streams = ExchangeFixture(), []
    def serve(request):
        response = fixture.serve(request)
        content = gzip.compress(response.content) if compressed else response.content
        body = TrackedStream([content[:11], content[11:]])
        streams.append(body)
        headers = dict(response.headers)
        if compressed:
            headers['Content-Encoding'] = 'gzip'
            headers['Content-Length'] = str(len(content))
        return httpx.Response(200, stream=body, headers=headers)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = ExchangeSecurityMaster(client, now=lambda: NOW).fetch('2026-11-06')
    assert result['rows'] == fetch(ExchangeFixture())['rows']
    assert result['expected_count'] == 4
    assert all(body.closed for body in streams)
    # Audit bytes/digests describe decoded content, as with previous eager HTTPX reads.
    from hashlib import sha256
    expected = json.dumps({'result': fixture.sse_current, 'pageHelp': {'total': 3}},
                          separators=(',', ':'), ensure_ascii=False).encode()
    assert result['raw']['requests'][0]['bytes'] == len(expected)
    assert result['raw']['requests'][0]['sha256'] == sha256(expected).hexdigest()


@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('status', [200, 206])
def test_stream_limit_caps_raw_and_decoded_body_without_caching(monkeypatch, compressed, status):
    monkeypatch.setattr('app.services.market_universe._MAX_WORKBOOK_BYTES', 64)
    content = b'x' * 100
    body = TrackedStream([gzip.compress(content) if compressed else content])
    headers = {'Content-Encoding': 'gzip'} if compressed else {}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            status, stream=body, headers=headers))) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.code == 'response_too_large' and error.value.status_code == status
        assert f'source_code=response_too_large http_status={status}' in source_failure_details(error.value)
        assert master._catalogue is None
    assert body.closed and body.pulls == 1


def test_stream_read_timeout_retains_transport_diagnostic_and_closes():
    body = TrackedStream([b'{', httpx.ReadTimeout('private response detail')])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.code == 'network_error'
        assert error.value.status_code == 200
        assert error.value.source_diagnostics['transport_category'] == 'read_timeout'
        assert 'http_status=200 transport_category=read_timeout' in source_failure_details(error.value)
        assert 'private response detail' not in source_failure_details(error.value)
        assert master._catalogue is None and master._catalogue_deadline is None
    assert body.closed and body.pulls == 2


def test_stream_decoding_failure_retains_received_http_status_and_closes():
    body = TrackedStream([b'not a valid gzip stream'])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, stream=body, headers={'Content-Encoding': 'gzip'}))) as client:
        master = ExchangeSecurityMaster(client, now=lambda: NOW)
        with pytest.raises(UniverseDataError) as error:
            master.fetch('2026-11-06')
        assert error.value.code == 'network_error' and error.value.status_code == 200
        assert error.value.source_diagnostics['transport_category'] == 'decoding_error'
        assert 'http_status=200 transport_category=decoding_error' in source_failure_details(error.value)
        assert master._catalogue is None
    assert body.closed and body.pulls == 1


def test_connect_failure_does_not_invent_response_status():
    def fail(request):
        raise httpx.ConnectError('no headers received')
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(UniverseDataError) as error:
            ExchangeSecurityMaster(client, now=lambda: NOW).fetch('2026-11-06')
    assert error.value.status_code is None
    assert 'http_status=- transport_category=connect_error' in source_failure_details(error.value)


def test_paced_stream_cancellation_propagates_unchanged_and_closes(tmp_path):
    from app.services.market_collection import CollectionCancelled, PacedHTTPClient
    from test_market_collection import worker
    collection, _, _, _ = worker(tmp_path)
    stop, cancellation = [False], CollectionCancelled()
    def check():
        if stop[0]:
            raise cancellation
    def cancel_during_chunk():
        stop[0] = True
        return b'{'
    body = TrackedStream([cancel_during_chunk, b'never read'])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))) as client:
        master = ExchangeSecurityMaster(PacedHTTPClient(client, collection, check), now=lambda: NOW)
        with pytest.raises(CollectionCancelled) as error:
            master.fetch('2026-11-06')
        assert error.value is cancellation and not hasattr(error.value, 'source_diagnostics')
        assert master._catalogue is None and master._catalogue_deadline is None
        assert not client.is_closed
    assert body.closed and body.pulls == 1
