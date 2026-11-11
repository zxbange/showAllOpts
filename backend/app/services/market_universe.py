"""Exchange-sourced SSE/SZSE security master, including CDRs and delistings.

Membership is selected from exchange listing intervals, never a hardcoded list
or a current quote feed. ``listed_until`` / ``delisted_on`` are EXCLUSIVE dates,
not the final trading session. Listing suspension is not a daily trading halt.

The public master is a current catalogue with listing/delisting dates, not a
versioned historical security database. A reconstructed past universe therefore
retains an explicit relisting/history limitation. In particular, the earliest
listing date is NOT advertised as a completeness/coverage start date.
"""
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import logging
import re
import time
from zipfile import BadZipFile
from zoneinfo import ZoneInfo

import httpx
from openpyxl import load_workbook

from app.services.market_diagnostics import record_transport_failure, source_failure_context


SSE_MASTER_URL = 'https://query.sse.com.cn/sseQuery/commonQuery.do'
SSE_DELIST_URL = 'https://query.sse.com.cn/commonQuery.do'
SZSE_METADATA_URL = 'https://www.szse.cn/api/report/ShowReport/data'
SZSE_DOWNLOAD_URL = 'https://www.szse.cn/api/report/ShowReport'
SSE_SQL_ID = 'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L'
SCOPE = 'SSE_SZSE_A_SHARE_CDR'
# Bound individual response bodies on slow exchange connections. Explicitly
# request one page, rather than the service's optional multi-page cache window.
_SSE_PAGE_SIZE = 20
_MAX_SECURITIES = 100000
_MAX_WORKBOOK_BYTES = 25 * 1024 * 1024
_REQUEST_BUDGET = 120.0
_CATALOGUE_BUDGET = 600.0
_SHANGHAI = ZoneInfo('Asia/Shanghai')
LOG = logging.getLogger(__name__)


class _GuardedStream(httpx.SyncByteStream):
    """Check raw chunks too, including compressed chunks emitting no output."""

    def __init__(self, iterator, check, status_code):
        self.iterator, self.check, self.status_code = iterator, check, status_code

    def __iter__(self):
        size = 0
        while True:
            self.check()
            try:
                chunk = next(self.iterator)
            except StopIteration:
                self.check()
                return
            self.check()
            size += len(chunk)
            if size > _MAX_WORKBOOK_BYTES:
                raise UniverseDataError('交易所股票名单响应超出大小限制。',
                                        code='response_too_large', status_code=self.status_code)
            yield chunk


class _ResponseBody:
    """Already decoded bytes, never passed through content decoding twice."""

    def __init__(self, status_code, content):
        self.status_code, self.content = status_code, content

    def json(self):
        return json.loads(self.content)


class UniverseDataError(ValueError):
    """An incomplete or ambiguous master must not silently shrink the universe."""

    def __init__(self, message, *, code='invalid_universe', status_code=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.is_rate_limited = status_code in (403, 429)


def _day(value, *, source=False, optional=False):
    if optional and value in (None, '', '-'):
        return None
    if isinstance(value, datetime):
        if not source or value.time().isoformat() != '00:00:00':
            raise UniverseDataError('上市日期不能包含时刻。', code='invalid_date')
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value) if isinstance(value, str) else ''
    if source and re.fullmatch(r'\d{8}', text):
        text = f'{text[:4]}-{text[4:6]}-{text[6:]}'
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        raise UniverseDataError('交易所上市日期缺失或格式无效。', code='invalid_date')
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UniverseDataError('交易所上市日期无效。', code='invalid_date') from None


def _count(value, *, zero=True):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not re.fullmatch(r'\d+', str(value))):
        raise UniverseDataError('交易所记录总数无效。', code='invalid_count')
    result = int(value)
    if not (0 if zero else 1) <= result <= _MAX_SECURITIES:
        raise UniverseDataError('交易所记录总数越界。', code='invalid_count')
    return result


def _code(value):
    # Excel may encode leading-zero stock codes as numeric cells.
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value).zfill(6)
    if not isinstance(value, str) or not re.fullmatch(r'\d{6}', value):
        raise UniverseDataError('交易所证券代码无效。', code='invalid_symbol')
    return value


def _header(value):
    return re.sub(r'\s+', '', str(value or '')).replace('<br>', '')


def _row(exchange, code, name, start, end, kind, source_id, raw, **extra):
    code = _code(code)
    if not isinstance(name, str) or not name.strip() or name == '-':
        raise UniverseDataError('交易所证券名称缺失。', code='invalid_name')
    start, end = _day(start, source=True), _day(end, source=True, optional=True)
    if end is not None and end <= start:
        raise UniverseDataError('上市和退市日期区间无效。', code='invalid_interval')
    return {
        'symbol': exchange + code, 'code': code, 'name': name.strip(),
        'exchange': 'SSE' if exchange == 'sh' else 'SZSE',
        'security_type': kind, 'type': kind,
        'listed_from': start.isoformat(),
        'listed_until': end.isoformat() if end else None,
        'delisted_on': end.isoformat() if end else None,
        'listed_until_exclusive': True,
        'evidence': {'source_id': source_id, 'listing_dates_verified': True,
                     'listing_verified': True, 'name_basis': 'latest_exchange_name', 'raw': raw},
        **extra,
    }


class ExchangeSecurityMaster:
    """Fetch a complete, counted current+delisted exchange catalogue.

    Injected httpx clients remain owned by the caller. No retries, alternate
    hosts, or protocol fallbacks are attempted after access denial/rate limiting.
    A fully validated catalogue may be reused within this instance on its
    Shanghai capture date. Each target date still gets its own membership
    selection and evidence; no catalogue is persisted across runs.
    """

    def __init__(self, client=None, *, timeout=None, now=None, monotonic=None):
        self.client = client or httpx.Client()
        self._owns_client = client is None
        self.timeout = httpx.Timeout(30.0, read=60.0) if timeout is None else timeout
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._catalogue_deadline = None
        self._catalogue = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._catalogue = None
        if self._owns_client:
            self.client.close()

    def _check_budget(self, request_deadline=None, status_code=None):
        elapsed = self._monotonic()
        if self._catalogue_deadline is not None and elapsed >= self._catalogue_deadline:
            raise UniverseDataError('证券主表采集超过总时限，已停止本次采集。',
                                    code='catalogue_deadline', status_code=status_code)
        if request_deadline is not None and elapsed >= request_deadline:
            raise UniverseDataError('交易所名单响应超过时限，已停止本次采集。',
                                    code='request_deadline', status_code=status_code)

    @contextmanager
    def _catalogue_budget(self):
        self._catalogue_deadline = self._monotonic() + _CATALOGUE_BUDGET
        try:
            yield
            self._check_budget()
        finally:
            self._catalogue_deadline = None

    def _read_body(self, response, request_deadline):
        check = lambda: self._check_budget(request_deadline, response.status_code)
        check()
        # Mock transports may supply already buffered/decoded responses. Real
        # streams are guarded before decompression, so compressed keep-alive
        # trickles cannot evade the elapsed checks by yielding no decoded bytes.
        decoded = None
        if response.is_stream_consumed:
            chunks = response.iter_bytes()
        else:
            decoded = httpx.Response(response.status_code, headers=response.headers,
                stream=_GuardedStream(iter(response.iter_raw()), check, response.status_code))
            chunks = decoded.iter_bytes()
        content = bytearray()
        try:
            for chunk in chunks:
                check()
                if len(content) + len(chunk) > _MAX_WORKBOOK_BYTES:
                    raise UniverseDataError('交易所股票名单响应超出大小限制。',
                                            code='response_too_large', status_code=response.status_code)
                content.extend(chunk)
            check()
        finally:
            if decoded is not None:
                decoded.close()
        return _ResponseBody(response.status_code, bytes(content))

    @source_failure_context(stage='request')
    def _request(self, url, params, requests):
        # Elapsed budgets are cooperative, not hard wall-clock deadlines. An
        # in-flight DNS/connect/header/read operation finishes or reaches its
        # HTTPX phase timeout before the next check. No threads or retries are
        # used. The paced stream also checks cancellation around raw chunks.
        self._check_budget()
        request_deadline = self._monotonic() + _REQUEST_BUDGET
        headers = {'User-Agent': 'Mozilla/5.0'}
        if 'sse.com.cn' in url:
            headers['Referer'] = 'https://www.sse.com.cn/assortment/stock/list/share/'
        status_code = None
        try:
            with self.client.stream('GET', url, params=params, headers=headers,
                                    timeout=self.timeout, follow_redirects=False) as response:
                status_code = response.status_code
                # Refuse at headers, without waiting for an error body or
                # redirect. PacedHTTPClient may already have raised on denial.
                if response.status_code in (403, 429):
                    raise UniverseDataError('交易所限制访问，已停止本次采集。',
                                            code='rate_limited', status_code=response.status_code)
                if not 200 <= response.status_code < 300:
                    raise UniverseDataError('交易所股票名单 HTTP 请求未成功。',
                                            code='http_error', status_code=response.status_code)
                response = self._read_body(response, request_deadline)
        except httpx.HTTPError as error:
            raise record_transport_failure(error,
                UniverseDataError('交易所股票名单请求失败。', code='network_error',
                                  status_code=status_code)) from None
        requests.append({'url': url, 'params': dict(params),
                         'status_code': response.status_code,
                         'sha256': sha256(response.content).hexdigest(),
                         'bytes': len(response.content)})
        return response

    @source_failure_context(stage='json_parse')
    def _json(self, url, params, requests):
        response = self._request(url, params, requests)
        try:
            return response.json()
        except ValueError:
            raise UniverseDataError('交易所股票名单不是有效 JSON。', code='invalid_json') from None

    @source_failure_context(stage='rows_validation')
    def _sse(self, *, delisted, requests):
        url = SSE_DELIST_URL if delisted else SSE_MASTER_URL
        params = {'STOCK_TYPE': '1,8', 'sqlId': SSE_SQL_ID,
                  'COMPANY_STATUS': '3' if delisted else '2,4,5,7,8',
                  'type': 'inParams', 'isPagination': 'true',
                  'pageHelp.pageSize': str(_SSE_PAGE_SIZE), 'pageHelp.cacheSize': '1'}
        source_id = 'sse_delisted' if delisted else 'sse_current'
        page, count, raw_rows = 1, None, []
        result, symbols = [], set()
        while count is None or len(raw_rows) < count:
            for key in ('pageNo', 'beginPage', 'endPage'):
                params['pageHelp.' + key] = str(page)
            payload = self._json(url, params, requests)
            with source_failure_context(stage='page_validation'):
                if not isinstance(payload, dict) or not isinstance(payload.get('pageHelp'), dict):
                    raise UniverseDataError('上交所分页元数据缺失。', code='invalid_page')
                if payload.get('actionErrors') or payload.get('fieldErrors'):
                    raise UniverseDataError('上交所名单查询返回错误。', code='invalid_page')
                meta = payload['pageHelp']
                total = _count(meta.get('total'), zero=delisted)
                if count is not None and count != total:
                    raise UniverseDataError('上交所名单采集期间总数变化。', code='count_changed')
                count = total
                # Some responses omit these fields. When supplied, they must
                # describe this request, not a repeated or enlarged page.
                for key, expected in (('pageNo', page), ('pageSize', _SSE_PAGE_SIZE),
                                      ('pageCount', (count + _SSE_PAGE_SIZE - 1) // _SSE_PAGE_SIZE)):
                    if key in meta and _count(meta[key]) != expected:
                        raise UniverseDataError('上交所分页元数据与请求不符。', code='invalid_page')
                rows = payload.get('result')
                if not isinstance(rows, list) or len(rows) != min(_SSE_PAGE_SIZE, count - len(raw_rows)):
                    raise UniverseDataError('上交所名单分页不完整。', code='incomplete_page')
                # The API can mirror the same page in both locations. It is
                # one page, never two sets of securities to concatenate.
                if 'data' in meta and meta['data'] != rows:
                    raise UniverseDataError('上交所分页记录互相矛盾。', code='invalid_page')
            for raw in rows:
                if not isinstance(raw, dict) or raw.get('STOCK_TYPE') not in ('1', '8'):
                    raise UniverseDataError('上交所股票分类无效或混入非目标类型。', code='invalid_type')
                code = raw.get('A_STOCK_CODE')
                end = raw.get('DELIST_DATE')
                if delisted and end in (None, '', '-'):
                    raise UniverseDataError('退市记录缺少退市日期。', code='invalid_interval')
                if not delisted and end not in (None, '', '-'):
                    raise UniverseDataError('现有股票记录包含退市日期。', code='invalid_interval')
                # 689 is an SSE CDR code family, not an inclusion whitelist.
                kind = 'cdr' if isinstance(code, str) and code.startswith('689') else 'stock'
                row = _row('sh', code, raw.get('SEC_NAME_CN') if not delisted
                           else raw.get('COMPANY_ABBR'), raw.get('LIST_DATE'), end,
                           kind, source_id, raw, board=raw['STOCK_TYPE'])
                if row['symbol'] in symbols:
                    raise UniverseDataError(f'{source_id} 名单包含重复代码。', code='duplicate_symbol')
                symbols.add(row['symbol'])
                result.append(row)
            raw_rows.extend(rows)
            if page == 1 or page % 10 == 0 or len(raw_rows) == count:
                LOG.info('Market source master page source=%s page=%d rows=%d collected=%d expected=%d',
                         source_id, page, len(rows), len(raw_rows), count)
            page += 1
        if not delisted and {row['board'] for row in result} != {'1', '8'}:
            raise UniverseDataError('上交所名单缺少主板或科创板。', code='missing_market')
        self._unique(result, source_id)
        return result, {'source_id': source_id, 'record_count': count,
                        'pages': page - 1, 'raw_rows': raw_rows}

    @staticmethod
    def _unique(rows, source_id):
        symbols = [row['symbol'] for row in rows]
        if len(set(symbols)) != len(symbols):
            raise UniverseDataError(f'{source_id} 名单包含重复代码。', code='duplicate_symbol')

    @source_failure_context(stage='metadata_validation')
    def _sz_metadata(self, catalog, tab, requests):
        payload = self._json(SZSE_METADATA_URL,
                             {'CATALOGID': catalog, 'TABKEY': tab, 'PAGENO': '1'}, requests)
        if not isinstance(payload, list):
            raise UniverseDataError('深交所名单元数据无效。', code='invalid_page')
        matches = [item for item in payload if isinstance(item, dict)
                   and isinstance(item.get('metadata'), dict)
                   and item['metadata'].get('tabkey') == tab]
        if len(matches) != 1:
            raise UniverseDataError('深交所目标列表缺失或重复。', code='invalid_page')
        meta = matches[0]['metadata']
        if str(meta.get('catalogid')) != catalog:
            raise UniverseDataError('深交所名单分类不符。', code='invalid_page')
        count, size = _count(meta.get('recordcount')), _count(meta.get('pagesize'), zero=False)
        pages = _count(meta.get('pagecount'))
        data = matches[0].get('data')
        if (pages != (count + size - 1) // size or _count(meta.get('pageno'), zero=False) != 1
                or not isinstance(data, list) or len(data) != min(size, count)):
            raise UniverseDataError('深交所名单分页统计不完整。', code='incomplete_page')
        return meta

    @source_failure_context(stage='rows_validation')
    def _sz(self, catalog, tab, source_id, requests):
        before = self._sz_metadata(catalog, tab, requests)
        count = _count(before['recordcount'])
        raw_rows = []
        if count:
            response = self._request(SZSE_DOWNLOAD_URL,
                                     {'SHOWTYPE': 'xlsx', 'CATALOGID': catalog, 'TABKEY': tab}, requests)
            with source_failure_context(stage='workbook_parse'):
                if len(response.content) > _MAX_WORKBOOK_BYTES:
                    raise UniverseDataError('深交所工作簿超出大小限制。', code='invalid_workbook')
                try:
                    workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=True)
                    try:
                        sheet = workbook.active
                        # Official exports currently declare A1:A1 even for >2900
                        # rows. Trust parsed row records, not the stale dimension.
                        sheet.reset_dimensions()
                        iterator = iter(sheet.values)
                        headers = [_header(value) for value in next(iterator)]
                        if not headers or len(set(headers)) != len(headers):
                            raise UniverseDataError('深交所工作簿表头无效。', code='invalid_workbook')
                        for values in iterator:
                            if all(value in ('', None) for value in values):
                                continue
                            if len(raw_rows) >= count or len(values) != len(headers):
                                raise UniverseDataError('深交所工作簿记录数量不符。', code='incomplete_page')
                            raw_rows.append(dict(zip(headers, [
                                _day(value, source=True).isoformat()
                                if isinstance(value, (datetime, date)) else value for value in values
                            ])))
                    finally:
                        workbook.close()
                except (BadZipFile, KeyError, OSError, StopIteration, ValueError) as exc:
                    if isinstance(exc, UniverseDataError):
                        raise
                    raise UniverseDataError('深交所工作簿无法解析。', code='invalid_workbook') from None
        after = self._sz_metadata(catalog, tab, requests)
        if (_count(after['recordcount']) != count or len(raw_rows) != count
                or before.get('cols') != after.get('cols')):
            raise UniverseDataError('深交所名单缺失或采集期间发生变化。', code='count_changed')
        if source_id == 'szse_current' and not count:
            raise UniverseDataError('深交所 A 股列表为空。', code='missing_market')
        result = []
        for raw in raw_rows:
            if catalog == '1110':
                cdr = tab == 'tab3'
                code_key, name_key = ('CDR代码', 'CDR简称') if cdr else ('A股代码', 'A股简称')
                date_key = '上市日期' if cdr else 'A股上市日期'
                result.append(_row('sz', raw.get(code_key), raw.get(name_key),
                                   raw.get(date_key), None, 'cdr' if cdr else 'stock', source_id, raw))
            else:
                code = _code(raw.get('证券代码'))
                # The official terminated list includes B shares. Exclude the
                # documented SZ B-share family, never a whitelist of A stocks.
                if code.startswith('2'):
                    continue
                end = raw.get('终止上市日期') if tab == 'tab2' else None
                if tab == 'tab2' and end in ('', '-', None):
                    raise UniverseDataError('深交所退市日期缺失。', code='invalid_interval')
                result.append(_row('sz', code, raw.get('证券简称'), raw.get('上市日期'), end,
                                   'stock', source_id, raw,
                                   listing_suspended_on=(str(_day(raw.get('暂停上市日期'), source=True))
                                                         if tab == 'tab1' else None)))
        self._unique(result, source_id)
        return result, {'source_id': source_id, 'record_count': count,
                        'included_count': len(result), 'metadata_before': before,
                        'metadata_after': after, 'raw_rows': raw_rows}

    def _capture_time(self):
        captured = self._now()
        if not isinstance(captured, datetime) or captured.tzinfo is None:
            raise UniverseDataError('采集时刻必须包含时区。', code='invalid_date')
        return captured, captured.astimezone(_SHANGHAI).date()

    def _load_catalogue(self, captured, captured_day):
        requests, sources, all_rows = [], [], []
        for delisted in (False, True):
            with source_failure_context(source_id='sse_delisted' if delisted else 'sse_current'):
                rows, source = self._sse(delisted=delisted, requests=requests)
            all_rows.extend(rows)
            sources.append(source)
        for catalog, tab, name in [('1110', 'tab1', 'szse_current'),
                                   ('1110', 'tab3', 'szse_cdr'),
                                   ('1793_ssgs', 'tab2', 'szse_delisted'),
                                   ('1793_ssgs', 'tab1', 'szse_listing_suspended')]:
            with source_failure_context(source_id=name):
                rows, source = self._sz(catalog, tab, name, requests)
            all_rows.extend(rows)
            sources.append(source)
        intervals = defaultdict(list)
        for row in all_rows:
            intervals[row['symbol']].append(row)
        for symbol, history in sorted(intervals.items()):
            history.sort(key=lambda item: item['listed_from'])
            for previous, following in zip(history, history[1:]):
                if not previous['listed_until'] or previous['listed_until'] > following['listed_from']:
                    raise UniverseDataError(f'{symbol} 上市区间重叠，需核验重新上市历史。',
                                            code='ambiguous_listing_intervals')
        # Do not label a catalogue assembled across Shanghai midnight as a
        # verified snapshot of either day, or automatically retry that fetch.
        if self._capture_time()[1] != captured_day:
            raise UniverseDataError('证券主表采集跨越上海日期，需重新采集。', code='capture_date_changed')
        return {'captured': captured, 'captured_day': captured_day,
                'rows': all_rows, 'sources': sources, 'requests': requests}

    @source_failure_context(source_id='exchange_master', stage='catalogue_validation')
    def fetch(self, day):
        requested = _day(day)
        captured, captured_day = self._capture_time()
        if self._catalogue is not None and self._catalogue['captured_day'] != captured_day:
            self._catalogue = None
        if requested > captured_day:
            raise UniverseDataError('当前证券主表不能确认未来股票池。', code='future_universe')
        catalogue = self._catalogue
        if catalogue is None:
            with self._catalogue_budget():
                catalogue = self._load_catalogue(captured, captured_day)
        # Protect the private cache from mutations to rows, raw data or source
        # evidence made by a consumer of a previous target-date result.
        current = deepcopy(catalogue)
        all_rows = current['rows']
        rows = [row for row in sorted(all_rows, key=lambda item: (item['symbol'], item['listed_from']))
                if row['listed_from'] <= str(requested) and (
                    row['listed_until'] is None or str(requested) < row['listed_until'])]
        if not rows:
            raise UniverseDataError('目标日期没有可核验的上市证券。', code='empty_universe')
        self._catalogue = catalogue
        historical = requested < captured_day
        limitation = (
            '交易所现有及退市目录按上市日期和退市日期重建股票池；'
            '不提供完整版本化重新上市区间，不能单凭此目录证明任意历史日期全覆盖。'
        )
        return {
            'date': str(requested), 'rows': rows, 'expected_count': len(rows),
            'catalogue_rows': all_rows,
            'scope': SCOPE, 'source': '上海证券交易所、深圳证券交易所',
            'fetched_at': current['captured'].isoformat(), 'source_has_trading_date': False,
            'evidence': {'source_counts_verified': True, 'listing_dates_verified': True,
                         'current_catalogue_verified': True,
                         'historical_membership_verified': False,
                         'membership_verified': not historical,
                         'universe_verified': not historical,
                         'membership_basis': 'exchange_current_and_delisted_listing_intervals',
                         'snapshot_date': str(current['captured_day']), 'coverage_from': None,
                         'listed_until_exclusive': True},
            'limitations': [limitation] if historical else [],
            'raw': {'sources': current['sources'], 'requests': current['requests']},
        }
