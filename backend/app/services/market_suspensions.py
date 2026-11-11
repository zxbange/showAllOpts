"""Affirmative full-day suspension intervals from Eastmoney's public report.

The report's DATETIME filter is not an exact-day snapshot: it also returns later
events. Actual timestamps are intersected with the requested range. Intraday
halts and predictions are never treated as full-day evidence. A missing symbol
means UNKNOWN, not paused=0. Trading calendars are the caller's responsibility.
"""
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

import httpx

from app.services.market_universe import UniverseDataError, _count, _day


SUSPENSION_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
SUSPENSION_REPORT = 'RPT_CUSTOM_SUSPEND_DATA_INTERFACE'
_PAGE_SIZE = 500
_SHANGHAI = ZoneInfo('Asia/Shanghai')
_OPEN, _CLOSE = time(9, 30), time(15)


class SuspensionDataError(UniverseDataError):
    """Incomplete interval reports cannot prove missing daily prices are pauses."""


def _timestamp(value, *, optional=False):
    if optional and value in (None, ''):
        return None
    if not isinstance(value, str) or len(value) != 19:
        raise SuspensionDataError('停复牌时间缺失或格式无效。', code='invalid_suspension_time')
    try:
        result = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        raise SuspensionDataError('停复牌时间无效。', code='invalid_suspension_time') from None
    if result.strftime('%Y-%m-%d %H:%M:%S') != value:
        raise SuspensionDataError('停复牌时间无效。', code='invalid_suspension_time')
    return result.replace(tzinfo=_SHANGHAI)


def _symbol(raw):
    code, secucode = raw.get('SECURITY_CODE'), raw.get('SECUCODE')
    if (not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit()
            or not isinstance(secucode, str) or secucode[:7] != code + '.'):
        raise SuspensionDataError('停复牌证券代码不一致。', code='invalid_suspension_symbol')
    suffix = secucode[7:]
    if suffix == 'BJ':
        return None
    if suffix not in ('SH', 'SZ'):
        raise SuspensionDataError('停复牌交易市场无效。', code='invalid_suspension_symbol')
    return suffix.lower() + code


def _full_day_interval(raw, start, end, captured_day, closed_day):
    symbol = _symbol(raw)
    if symbol is None:
        return None
    begins = _timestamp(raw.get('SUSPEND_START_TIME'))
    finishes = _timestamp(raw.get('SUSPEND_END_TIME'), optional=True)
    declared = _timestamp(raw.get('SUSPEND_START_DATE'))
    if declared.date() != begins.date() or (finishes is not None and finishes < begins):
        raise SuspensionDataError('停复牌时间区间矛盾。', code='invalid_suspension_interval')
    # The first day is not fully suspended if there was any opening trading.
    first = begins.date() + (timedelta(days=1) if begins.time() > _OPEN else timedelta())
    if finishes:
        last = finishes.date() - (timedelta(days=1) if finishes.time() < _CLOSE else timedelta())
        basis = 'actual_end_timestamp'
    else:
        if raw.get('SUSPEND_EXPIRE') != '连续停牌':
            return None  # A one-day or intraday halt with no end is not proof.
        last, basis = captured_day, 'open_continuous_interval_as_observed'
        predicted = _timestamp(raw.get('PREDICT_RESUME_DATE'), optional=True)
        if predicted is not None:
            # A predicted resumption is not affirmative suspension evidence on
            # or after that date; bound conservatively, without claiming resume.
            last = min(last, predicted.date() - timedelta(days=1))
            basis = 'open_interval_bounded_before_predicted_resume'
    first, last = max(first, start), min(last, end, closed_day)
    if first > last:
        return None
    return symbol, {
        'start': first.isoformat(), 'end': last.isoformat(), 'end_inclusive': True,
        'paused': 1,
        'evidence': {'source': '东方财富停复牌信息', 'report': SUSPENSION_REPORT,
                     'full_day_verified': True, 'absence_means_unpaused': False,
                     'interval_basis': basis, 'observed_on': captured_day.isoformat(), 'raw': raw},
    }


class PublicSuspensions:
    def __init__(self, client=None, *, timeout=30.0, now=None):
        self.client = client or httpx.Client()
        self._owns_client = client is None
        self.timeout = timeout
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_client:
            self.client.close()

    def fetch(self, start, end):
        start, end = _day(start), _day(end)
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise SuspensionDataError('采集时刻必须包含时区。', code='invalid_date')
        captured_day = now.astimezone(_SHANGHAI).date()
        local_now = now.astimezone(_SHANGHAI)
        closed_day = captured_day - (timedelta(days=1) if local_now.time() < _CLOSE else timedelta())
        if start > end or end > captured_day:
            raise SuspensionDataError('停复牌查询区间无效或包含未来日期。', code='invalid_date')
        params = {'sortColumns': 'SUSPEND_START_DATE,SECURITY_CODE', 'sortTypes': '-1,1',
                  'pageSize': str(_PAGE_SIZE), 'pageNumber': '1',
                  'reportName': SUSPENSION_REPORT, 'columns': 'ALL',
                  'source': 'WEB', 'client': 'WEB',
                  'filter': f'(MARKET="全部")(DATETIME=\'{start.isoformat()}\')'}
        requests, rows, page, total, pages = [], [], 1, None, None
        seen = set()
        while total is None or len(rows) < total:
            params['pageNumber'] = str(page)
            try:
                response = self.client.get(SUSPENSION_URL, params=params, timeout=self.timeout,
                                           headers={'User-Agent': 'Mozilla/5.0'}, follow_redirects=False)
            except httpx.HTTPError:
                raise SuspensionDataError('停复牌接口网络请求失败。', code='network_error') from None
            requests.append({'url': SUSPENSION_URL, 'params': dict(params),
                             'status_code': response.status_code,
                             'sha256': sha256(response.content).hexdigest()})
            if response.status_code in (403, 429):
                raise SuspensionDataError('停复牌接口限制访问，已停止本次采集。',
                                          code='rate_limited', status_code=response.status_code)
            if not 200 <= response.status_code < 300:
                raise SuspensionDataError('停复牌接口 HTTP 请求未成功。',
                                          code='http_error', status_code=response.status_code)
            try:
                payload = response.json()
            except ValueError:
                raise SuspensionDataError('停复牌接口返回无效 JSON。', code='invalid_json') from None
            if (not isinstance(payload, dict) or payload.get('success') is not True
                    or not isinstance(payload.get('result'), dict)):
                raise SuspensionDataError('停复牌接口没有返回可核验报表。', code='invalid_report')
            report = payload['result']
            count, report_pages = _count(report.get('count')), _count(report.get('pages'))
            if report_pages != (count + _PAGE_SIZE - 1) // _PAGE_SIZE:
                raise SuspensionDataError('停复牌报表分页统计不完整。', code='incomplete_page')
            if total is not None and (total != count or pages != report_pages):
                raise SuspensionDataError('停复牌报表采集期间变化。', code='count_changed')
            total, pages = count, report_pages
            batch = report.get('data')
            if not isinstance(batch, list) or len(batch) != min(_PAGE_SIZE, total - len(rows)):
                raise SuspensionDataError('停复牌报表分页缺失。', code='incomplete_page')
            for raw in batch:
                if not isinstance(raw, dict):
                    raise SuspensionDataError('停复牌记录结构无效。', code='invalid_report')
                identity = (raw.get('SECUCODE'), raw.get('SUSPEND_START_TIME'), raw.get('SUSPEND_END_TIME'))
                if identity in seen:
                    raise SuspensionDataError('停复牌报表包含重复事件。', code='duplicate_event')
                seen.add(identity)
                rows.append(raw)
            page += 1
        intervals = {}
        for raw in rows:
            item = _full_day_interval(raw, start, end, captured_day, closed_day)
            if item is not None:
                symbol, interval = item
                intervals.setdefault(symbol, []).append(interval)
        for values in intervals.values():
            values.sort(key=lambda item: (item['start'], item['end']))
        return {'start': start.isoformat(), 'end': end.isoformat(), 'intervals': intervals,
                'source': '东方财富停复牌信息', 'fetched_at': now.isoformat(),
                'evidence': {'report_count_verified': True, 'affirmative_intervals_only': True,
                             'absence_means_unpaused': False, 'raw_count': total,
                             'latest_full_day_evidence_date': closed_day.isoformat(),
                             'calendar_verified': False, 'coverage_from': None},
                'raw': {'requests': requests, 'rows': rows}}
