"""Safe diagnostics expose controlled reasons, never upstream exception payloads."""
from datetime import date
from io import BytesIO

import httpx
import openpyxl
import pytest

from app.services import margin_source as source

DAY = date(2026, 10, 26)
URL = source.ORIGIN + '/cmsmc/articleFileDir/2026-10/26/sample.xlsx'
SECRET = 'private-password Authorization: Bearer token'


def workbook(day=DAY, value=2.55, fmt='0.0%'):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(['统计指标', day])
    sheet.append([source.METRIC, value])
    sheet['B2'].number_format = fmt
    output = BytesIO()
    book.save(output)
    return output.getvalue()


@pytest.mark.parametrize('status', [403, 429, 503])
def test_http_status_reason_without_body_headers_or_credentials(status):
    def respond(request):
        return httpx.Response(status, text=SECRET, headers={'x-private': SECRET})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(source.MarginSourceError) as caught:
            source.fetch_point(client, source.MarginReport(DAY, URL))
    details = source.safe_error_details(caught.value)
    assert details == dict(error_type='MarginSourceError', code='http_status',
                           reason=f'中证服务器返回 HTTP {status}。', http_status=status,
                           cause_type='HTTPStatusError')
    assert SECRET not in str(caught.value) + str(details)


@pytest.mark.parametrize('content,code,reason', [
    (b'<html>denied</html>', 'invalid_workbook', '有效 XLS/XLSX'),
    (workbook(date(2026, 10, 23)), 'report_date_mismatch', '统计日期'),
    (workbook(fmt='General'), 'missing_percent_unit', '百分比单位'),
])
def test_report_validation_details(content, code, reason):
    with pytest.raises(source.MarginSourceError) as caught:
        source.parse_report(content, DAY)
    details = source.safe_error_details(caught.value)
    assert details['code'] == code
    assert reason in details['reason']
    assert 'http_status' not in details


@pytest.mark.parametrize('error', [RuntimeError(SECRET), source.MarginSourceError(SECRET),
                                  source.MarginSourceError('中证服务器返回 HTTP 503。' + SECRET)])
def test_unrecognized_exception_strings_never_exposed(error):
    details = source.safe_error_details(error)
    assert details['code'] == 'unexpected_error'
    assert SECRET not in str(details)


def test_unusual_exception_type_name_is_bounded():
    error = type('Bad\n' + SECRET, (Exception,), {})(SECRET)
    assert source.safe_error_details(error)['error_type'] == 'Exception'


def test_network_and_workbook_exception_payloads_are_not_exposed(monkeypatch):
    def fail(request):
        raise httpx.ConnectError(SECRET, request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(source.MarginSourceError) as caught:
            source.fetch_point(client, source.MarginReport(DAY, URL))
    assert source.safe_error_details(caught.value)['code'] == 'network_error'
    assert SECRET not in str(caught.value)
    def broken(*args, **kwargs):
        raise AttributeError(SECRET)
    monkeypatch.setattr(source.openpyxl, 'load_workbook', broken)
    with pytest.raises(source.MarginSourceError) as caught:
        source.parse_report(workbook(), DAY)
    assert source.safe_error_details(caught.value)['code'] == 'workbook_parse'
    assert source.safe_error_details(caught.value)['cause_type'] == 'AttributeError'
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize('url', ['https://evil.test/sample.xlsx', URL + '?token=private',
                               URL + '#private', URL.replace('www.', 'user:password@www.'),
                               URL + '\n', URL.replace('sample', 'x' * 2048), None])
def test_diagnostic_urls_are_validated_and_bounded(url):
    assert source.safe_report_url(url) is None


def test_valid_official_report_url_is_retained():
    assert source.safe_report_url(URL) == URL


@pytest.mark.parametrize('error_type', [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError])
def test_network_cause_types_are_distinguishable_without_payloads(error_type):
    def fail(request):
        raise error_type(SECRET, request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(source.MarginSourceError) as caught:
            source.fetch_point(client, source.MarginReport(DAY, URL))
    details = source.safe_error_details(caught.value)
    assert details['code'] == 'network_error'
    assert details['cause_type'] == error_type.__name__
    assert SECRET not in str(caught.value) + str(details)


@pytest.mark.parametrize('cause_type', ['Bad\n' + SECRET, 'X' * 81, 123])
def test_cause_type_is_bounded_and_sanitized(cause_type):
    details = source.safe_error_details(source.MarginSourceError('中证报表解析失败。', cause_type=cause_type))
    assert 'cause_type' not in details
