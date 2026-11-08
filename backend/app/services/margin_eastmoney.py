"""Eastmoney market-wide maintenance ratio, in percentage points, not a fraction."""
from dataclasses import dataclass
from datetime import date
import json
import math
import re
import time

import httpx

from app.services.margin_source import MarginReport, MarginSourceError, fetch_point as official_point

API_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
REPORT_NAME = 'RPTA_WEB_MARGIN_DAILYTRADE'
PAGE_SIZE = 500
MAX_PAGES = 40
MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RatioReport(MarginReport):
    ratio: float | None


def report_url(day):
    return str(httpx.URL(API_URL, params={
        'reportName': REPORT_NAME, 'columns': 'STATISTICS_DATE,AVG_GUARANTEE_RATIO',
        'filter': f"(STATISTICS_DATE='{day.isoformat()}')",
    }))


def is_report_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = httpx.URL(url)
        match = re.fullmatch(r"\(STATISTICS_DATE='(\d{4}-\d{2}-\d{2})'\)", parsed.params.get('filter', ''))
        return bool(match and url == report_url(date.fromisoformat(match[1])))
    except (ValueError, httpx.InvalidURL):
        return False


def _page(client, start, end, page):
    params = dict(reportName=REPORT_NAME, columns='STATISTICS_DATE,AVG_GUARANTEE_RATIO',
                  pageNumber=page, pageSize=PAGE_SIZE, sortColumns='STATISTICS_DATE', sortTypes='-1',
                  filter=f"(STATISTICS_DATE>='{start.isoformat()}')(STATISTICS_DATE<='{end.isoformat()}')")
    started = time.monotonic()
    try:
        with client.stream('GET', API_URL, params=params, follow_redirects=False,
                           timeout=httpx.Timeout(25, connect=10)) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > MAX_BYTES or time.monotonic() - started > 60:
                    raise MarginSourceError('东方财富响应超过读取上限。')
                content.extend(chunk)
        data = json.loads(content)
    except httpx.HTTPStatusError as exc:
        raise MarginSourceError(f'东方财富服务器返回 HTTP {exc.response.status_code}。',
                                cause_type=type(exc).__name__) from None
    except httpx.HTTPError as exc:
        raise MarginSourceError('读取东方财富数据失败，请稍后重试。', cause_type=type(exc).__name__) from None
    except (ValueError, UnicodeError):
        raise MarginSourceError('东方财富返回的数据格式无效。') from None
    if not isinstance(data, dict):
        raise MarginSourceError('东方财富返回的数据格式无效。')
    # The provider explicitly uses 9201 for an empty date range.
    if data.get('code') == 9201 and data.get('success') is False and data.get('result') is None:
        return [], 0, 0
    result = data.get('result')
    if data.get('success') is not True or not isinstance(result, dict):
        raise MarginSourceError('东方财富接口未返回成功结果。')
    rows, pages, count = result.get('data'), result.get('pages'), result.get('count')
    if (not isinstance(rows, list) or type(pages) is not int or type(count) is not int
            or not 1 <= pages <= MAX_PAGES or not 0 < count <= MAX_PAGES * PAGE_SIZE
            or pages != (count + PAGE_SIZE - 1) // PAGE_SIZE):
        raise MarginSourceError('东方财富分页数据不完整或发生变化，请重试。')
    return rows, pages, count


def list_reports(client, start, end, check_cancel=lambda: None):
    """Read bounded date-filtered JSON pages; keep null observations pending."""
    if start > end:
        raise ValueError('开始日期不能晚于结束日期。')
    reports, expected, previous = [], None, None
    page = 1
    while True:
        check_cancel()
        rows, pages, count = _page(client, start, end, page)
        check_cancel()
        if expected is None:
            expected = (pages, count)
        if expected != (pages, count) or (count and not rows):
            raise MarginSourceError('东方财富分页数据不完整或发生变化，请重试。')
        for row in rows:
            if not isinstance(row, dict) or 'AVG_GUARANTEE_RATIO' not in row:
                raise MarginSourceError('东方财富返回的数据格式无效。')
            raw = row.get('STATISTICS_DATE')
            if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?: 00:00:00)?', raw):
                raise MarginSourceError('东方财富统计日期无效或超出请求范围。')
            try:
                day = date.fromisoformat(raw[:10])
            except ValueError:
                raise MarginSourceError('东方财富统计日期无效或超出请求范围。') from None
            if not start <= day <= end or (previous is not None and day >= previous):
                raise MarginSourceError('东方财富统计日期无效或超出请求范围。')
            ratio = row['AVG_GUARANTEE_RATIO']
            if ratio is not None and (type(ratio) not in (int, float) or not math.isfinite(ratio)
                                      or not 0 < ratio < 10000):
                raise MarginSourceError('东方财富担保比例不是有效百分比。')
            reports.append(RatioReport(day, report_url(day), ratio))
            previous = day
        if page >= pages:
            break
        page += 1
    if len(reports) != expected[1]:
        raise MarginSourceError('东方财富分页数据不完整或发生变化，请重试。')
    return sorted(reports, key=lambda item: item.trade_date)


def fetch_point(client, report):
    if not isinstance(report, RatioReport):
        if not is_report_url(report.url):
            # Resume legacy official-workbook checkpoints, including title corrections.
            return official_point(client, report)
        rows = list_reports(client, report.trade_date, report.trade_date)
        report = rows[0] if rows else RatioReport(report.trade_date, report.url, None)
    if report.ratio is None:
        raise MarginSourceError('东方财富尚未披露该日担保比例，将继续补采。')
    return dict(date=report.trade_date.isoformat(), ratio=report.ratio, source_url=report.url)
