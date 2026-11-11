"""Public exchange contracts; all requests and securities are synthetic/offline."""
from copy import deepcopy
from datetime import date, datetime, timezone
from io import BytesIO
import json
import re
from zipfile import ZipFile

import httpx
from openpyxl import Workbook
import pytest

from app.services.market_universe import (
    ExchangeSecurityMaster, SSE_MASTER_URL, SZSE_METADATA_URL, UniverseDataError,
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
            return httpx.Response(200, json={'result': rows, 'pageHelp': {'total': len(rows)}})
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
