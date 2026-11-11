"""Official target-date category counts: a guard, never membership evidence.

Counts cannot identify historical constituents, prove suspension status, or prove
JoinQuant equivalence. Both exchanges must return the exact requested date.
HTTP access denials stop immediately, without retry, redirects or host fallback.

Only the observed report schemas are supported, not every historical exchange
classification. Explicit zero counts are valid; absent categories are never
inferred to be zero. Historical SZSE SME-board rows require separately verified
grouping rules and are rejected rather than silently omitted or added to main A.
"""
from hashlib import sha256
from html import unescape
import re

import httpx

from app.services.market_sina import SinaDataError, SinaRateLimitError, _day


SSE_COUNTS_URL = 'https://query.sse.com.cn/commonQuery.do'
SZSE_COUNTS_URL = 'https://www.szse.cn/api/report/ShowReport/data'
SSE_SQL_ID = 'COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C'


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SinaDataError('交易所股票数量类型无效。', code='invalid_count')
    text = str(value)
    if not re.fullmatch(r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)', text):
        raise SinaDataError('交易所股票数量格式无效。', code='invalid_count')
    number = int(text.replace(',', ''))
    if not 0 <= number <= 100000:
        raise SinaDataError('交易所股票数量超出范围。', code='invalid_count')
    return number


def _sse(payload, day):
    if (not isinstance(payload, dict) or payload.get('actionErrors')
            or payload.get('fieldErrors') or payload.get('sqlId') != SSE_SQL_ID
            or payload.get('isPagination') not in (False, 'false')
            or not isinstance(payload.get('result'), list)):
        raise SinaDataError('上交所统计响应无效。', code='invalid_report')
    categories = {'01': 'sse_main_a', '03': 'sse_star'}
    counts, seen = {}, set()
    for row in payload['result']:
        if not isinstance(row, dict) or not isinstance(row.get('PRODUCT_CODE'), str):
            raise SinaDataError('上交所统计行无效。', code='invalid_report')
        category = row['PRODUCT_CODE']
        if category in seen:
            raise SinaDataError('上交所统计类别重复。', code='duplicate_category')
        seen.add(category)
        if row.get('TRADE_DATE') != day.strftime('%Y%m%d'):
            raise SinaDataError('上交所统计日期不符。', code='count_date_mismatch')
        if category in categories:
            counts[categories[category]] = _count(row.get('LIST_NUM'))
    if set(counts) != set(categories.values()):
        raise SinaDataError('上交所统计类别缺失。', code='missing_category')
    return counts


def _szse(payload, day):
    if not isinstance(payload, list):
        raise SinaDataError('深交所统计响应无效。', code='invalid_report')
    reports = [item for item in payload if isinstance(item, dict)
               and isinstance(item.get('metadata'), dict)
               and item['metadata'].get('catalogid') == '1803_sczm'
               and item['metadata'].get('tabkey') == 'tab1']
    if len(reports) != 1:
        raise SinaDataError('深交所统计报表缺失或重复。', code='invalid_report')
    report = reports[0]
    metadata = report['metadata']
    if (report.get('error') is not None or 'error' not in report
            or metadata.get('pagesize') != -1 or not isinstance(report.get('data'), list)):
        raise SinaDataError('深交所统计响应无效或不完整。', code='invalid_report')
    if metadata.get('subname') != day.isoformat():
        raise SinaDataError('深交所统计日期不符。', code='count_date_mismatch')
    categories = {'主板A股': 'szse_main_a', '创业板A股': 'szse_chinext'}
    counts, seen = {}, set()
    for row in report['data']:
        if not isinstance(row, dict) or not isinstance(row.get('lbmc'), str):
            raise SinaDataError('深交所统计行无效。', code='invalid_report')
        category = unescape(row['lbmc']).strip()
        if '中小' in category:
            raise SinaDataError('深交所历史中小板分类尚未核验，不能合并统计。',
                                code='unsupported_historical_schema')
        if category in seen:
            raise SinaDataError('深交所统计类别重复。', code='duplicate_category')
        seen.add(category)
        if category in categories:
            counts[categories[category]] = _count(row.get('zqsl'))
    if set(counts) != set(categories.values()):
        raise SinaDataError('深交所统计类别缺失。', code='missing_category')
    return counts


class ExchangeDailyCounts:
    def __init__(self, client=None, *, timeout=30.0,
                 sse_url=SSE_COUNTS_URL, szse_url=SZSE_COUNTS_URL):
        self.client = client if client is not None else httpx.Client()
        self._owns_client = client is None
        self.timeout = timeout
        self.sse_url, self.szse_url = sse_url, szse_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_client:
            self.client.close()

    def _request(self, url, params, referer):
        try:
            response = self.client.get(url, params=params, timeout=self.timeout,
                                       headers={'Referer': referer, 'User-Agent': 'Mozilla/5.0'},
                                       follow_redirects=False)
        except httpx.HTTPError:
            raise SinaDataError('交易所统计接口网络请求失败。', code='network_error') from None
        if response.status_code in (403, 429):
            raise SinaRateLimitError(response.status_code,
                                     retry_after=response.headers.get('Retry-After'))
        if not 200 <= response.status_code < 300:
            raise SinaDataError('交易所统计接口 HTTP 请求未成功。', code='http_error',
                                status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise SinaDataError('交易所统计接口返回无效 JSON。', code='invalid_json') from None
        evidence = {'source_url': str(response.url), 'params': dict(params),
                    'status_code': response.status_code,
                    'sha256': sha256(response.content).hexdigest(), 'raw': payload}
        return payload, evidence

    def fetch(self, day):
        day = _day(day)
        sse, sse_evidence = self._request(self.sse_url, {
            'sqlId': SSE_SQL_ID, 'PRODUCT_CODE': '01,02,03,11,17',
            'type': 'inParams', 'SEARCH_DATE': day.isoformat()}, 'https://www.sse.com.cn/')
        counts = _sse(sse, day)
        szse, szse_evidence = self._request(self.szse_url, {
            'SHOWTYPE': 'JSON', 'CATALOGID': '1803_sczm',
            'txtQueryDate': day.isoformat(), 'TABKEY': 'tab1'}, 'https://www.szse.cn/')
        counts.update(_szse(szse, day))
        return {'date': day.isoformat(), 'counts': counts, 'verified': True,
                'evidence': {'sse': sse_evidence, 'szse': szse_evidence,
                             'target_date_verified': True, 'membership_verified': False,
                             'historical_schema_coverage': 'observed categories only; absent categories and separate SME grouping unsupported',
                             'scope': 'SSE main A + STAR; SZSE main A + ChiNext; excludes B shares and BSE'}}
