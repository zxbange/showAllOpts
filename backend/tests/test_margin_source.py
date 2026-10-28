"""Official XLS/XLSX layouts and bounded HTTP behavior; never fetch live data."""
from datetime import date, datetime
from io import BytesIO
from types import SimpleNamespace

import httpx
import openpyxl
import pytest

from app.services import margin_source as source


DAY = date(2026, 10, 26)
REPORT_URL = source.ORIGIN + '/cmsmc/articleFileDir/2026-10/26/sample.xlsx'


def workbook(value=2.556273, fmt='#,##0.##%', day=DAY, label=source.METRIC):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet['A1'], sheet['A2'] = source.TITLE, '统计指标'
    sheet['C2'] = datetime.combine(day, datetime.min.time()) if day else '日期缺失'
    sheet['B19'], sheet['C19'] = '担保物总价值（亿元）', 72774.01291568
    sheet['B20'], sheet['C20'] = label, value
    sheet['C20'].number_format = fmt
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def listing(dates, next_path=None, extension='xlsx'):
    links = ['<li><span>2099-01-01</span><a title="{}{}" href="/cmsmc/articleFileDir/sample-{}.{}">{}</a></li>'.format(
        source.TITLE, day.strftime('%Y%m%d'), day.isoformat(), extension, source.TITLE) for day in dates]
    if next_path:
        links.append('<a onclick="queryArticleByCondition(this,\'{}\')">下页</a>'.format(next_path))
    return ('<html>' + ''.join(links) + '</html>').encode()


def test_current_xlsx_uses_percent_format_and_report_date():
    assert source.parse_report(workbook(), DAY) == pytest.approx(255.6273)


def test_old_report_text_percent_is_not_multiplied_twice():
    assert source.parse_report(workbook('272.9%', '0.0%', date(2020, 10, 27)), date(2020, 10, 27)) == 272.9


def test_numeric_percentage_with_explicit_label_unit():
    assert source.parse_report(workbook(272.9, 'General', label=source.METRIC+'（%）'), DAY) == 272.9


@pytest.mark.parametrize('value,fmt', [(2.55, 'General'), ('--', '0.0%'), (None, '0.0%'), (0, '0.0%'),
                                       (True, '0.0%'), ('NaN%', '0.0%'), (2.55, '0.0"%"')])
def test_unknown_units_and_missing_values_are_errors(value, fmt):
    with pytest.raises(source.MarginSourceError):
        source.parse_report(workbook(value, fmt), DAY)


@pytest.mark.parametrize('day', [date(2026, 10, 23), None])
def test_date_mismatch_or_missing_never_becomes_an_observation(day):
    with pytest.raises(source.MarginSourceError, match='日期'):
        source.parse_report(workbook(day=day), DAY)


def test_xls_reader_preserves_legacy_string_values_and_number_format(monkeypatch):
    # The official 2020-10-27 XLS has a string date in D2 and '272.9%' in D20.
    def cell(value, ctype=1):
        return SimpleNamespace(value=value, ctype=ctype, xf_index=0)
    rows = [[cell('统计指标'), cell(''), cell(''), cell('2020年10月27日')],
            [cell(''), cell(source.METRIC), cell(''), cell('272.9%')]]
    released = []
    sheet = SimpleNamespace(nrows=len(rows), ncols=4, row=lambda i: rows[i])
    book = SimpleNamespace(sheets=lambda: [sheet], xf_list=[SimpleNamespace(format_key=0)],
                           format_map={0: SimpleNamespace(format_str='0.0%')}, datemode=0,
                           release_resources=lambda: released.append(True))
    monkeypatch.setattr(source.xlrd, 'open_workbook', lambda **kwargs: book)
    assert source.parse_report(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', date(2020, 10, 27)) == 272.9
    assert released == [True]


def test_missing_metric_and_invalid_file_are_not_zero():
    with pytest.raises(source.MarginSourceError, match='唯一'):
        source.parse_report(workbook(label='融资融券余额'), DAY)
    with pytest.raises(source.MarginSourceError, match='XLS'):
        source.parse_report(b'<html>request denied</html>', DAY)


def test_listing_uses_title_not_publication_date_and_real_next_link():
    next_path = source.LIST_PATHS[0]+'d2bb5c19-2.html'
    reports, next_url = source.parse_listing(listing([DAY], next_path), source.INDEX_URL)
    assert reports[0].trade_date == DAY
    assert next_url == source.ORIGIN + next_path


def test_list_reports_stops_at_date_boundary_and_checks_cancellation():
    next_path = source.LIST_PATHS[0]+'d2bb5c19-2.html'
    visited, cancelled = [], []
    def response(request):
        visited.append(str(request.url))
        if str(request.url) == source.INDEX_URL:
            return httpx.Response(200, content=listing([DAY], next_path))
        if str(request.url) == source.ORIGIN+next_path:
            return httpx.Response(200, content=listing([date(2026, 10, 23)], source.LIST_PATHS[0]+'d2bb5c19-3.html'))
        if str(request.url) == source.HISTORY_URL:
            return httpx.Response(200, content=listing([date(2025, 2, 21)], source.LIST_PATHS[1]+'archive-2.html'))
        pytest.fail('Must not request pages older than the requested start date')
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        reports = source.list_reports(client, date(2026, 10, 24), DAY, lambda: cancelled.append(True))
    assert [r.trade_date for r in reports] == [DAY]
    assert len(visited) == len(cancelled) == 3


def test_duplicate_archive_day_keeps_current_report():
    def response(request):
        suffix = 'xlsx' if str(request.url) == source.INDEX_URL else 'xls'
        return httpx.Response(200, content=listing([DAY], extension=suffix))
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        reports = source.list_reports(client, DAY, DAY)
    assert len(reports) == 1 and reports[0].url.endswith('.xlsx')


def test_cyclic_pagination_is_rejected():
    def response(request):
        return httpx.Response(200, content=listing([DAY], source.LIST_PATHS[0]+'index.html'))
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(source.MarginSourceError, match='循环'):
            source.list_reports(client, DAY, DAY)


def test_empty_changed_or_unsorted_catalog_does_not_mean_no_new_data():
    with pytest.raises(source.MarginSourceError, match='目录未找到'):
        source.parse_listing(b'<html>unavailable</html>', source.INDEX_URL)
    with pytest.raises(source.MarginSourceError, match='日期顺序'):
        source.parse_listing(listing([date(2026, 10, 23), DAY]), source.INDEX_URL)


@pytest.mark.parametrize('url', [
    'http://www.csdata.cn/cmsmc/articleFileDir/sample.xls',
    'https://www.csdata.cn.evil.invalid/cmsmc/articleFileDir/sample.xls',
    'https://www.csdata.cn@evil.invalid/cmsmc/articleFileDir/sample.xls',
    'https://www.csdata.cn/cmsmc/articleFileDir/%2e%2e/secrets.xls',
    'https://www.csdata.cn/cmsmc/articleFileDir/a.xls?redirect=evil',
    'https://www.csdata.cn/other/a.xls',
])
def test_source_whitelist_rejects_unsafe_urls_before_any_request(url):
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail('unsafe request'))) as client:
        with pytest.raises(source.MarginSourceError):
            source.fetch_point(client, source.MarginReport(DAY, url))


def test_cross_domain_redirect_is_not_followed():
    visited = []
    def response(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://example.invalid/data.xlsx'})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(source.MarginSourceError):
            source.fetch_point(client, source.MarginReport(DAY, REPORT_URL))
    assert visited == [REPORT_URL]


def test_fetch_point_returns_percentage_and_provenance():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=workbook()))) as client:
        point = source.fetch_point(client, source.MarginReport(DAY, REPORT_URL))
    assert point == dict(date='2026-10-26', ratio=255.6273, source_url=REPORT_URL)


def test_http_errors_and_oversized_responses_are_explicit():
    for response in [httpx.Response(503), httpx.Response(200, headers={'content-length': str(source.MAX_FILE_BYTES+1)})]:
        with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
            with pytest.raises(source.MarginSourceError):
                source.fetch_point(client, source.MarginReport(DAY, REPORT_URL))


def test_cancellation_happens_before_network_request():
    def cancel():
        raise RuntimeError('cancelled')
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('unexpected request'))) as client:
        with pytest.raises(RuntimeError, match='cancelled'):
            source.list_reports(client, DAY, DAY, cancel)
