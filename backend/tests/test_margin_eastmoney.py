from datetime import date

import httpx
import pytest

from app.services import margin_eastmoney as source
from app.services.margin_source import MarginSourceError, safe_error_details, safe_report_url


DAY = date(2026, 11, 5)


def payload(rows, pages=1, count=None):
    return dict(success=True, code=0, result=dict(data=rows, pages=pages,
                                                count=len(rows) if count is None else count))


def row(day=DAY, ratio=262.6319):
    return dict(STATISTICS_DATE=str(day)+' 00:00:00', AVG_GUARANTEE_RATIO=ratio)


def test_batch_records_need_no_per_day_requests_and_keep_percent_units():
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url.params['reportName'] == source.REPORT_NAME
        assert "STATISTICS_DATE>='2026-11-04'" in request.url.params['filter']
        return httpx.Response(200, json=payload([row(), row(date(2026, 11, 4), 259.3444)]))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reports = source.list_reports(client, date(2026, 11, 4), DAY)
        points = [source.fetch_point(client, report) for report in reports]
    assert len(requests) == 1
    assert points[-1]['ratio'] == 262.6319
    assert points[-1]['date'] == str(DAY)
    assert safe_report_url(points[-1]['source_url']) == source.report_url(DAY)
    assert safe_report_url(points[-1]['source_url']+'&token=secret') is None


@pytest.mark.parametrize('bad', [True, '262.63', -1, 0, 10000])
def test_bad_values_are_not_guessed_or_published(bad):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload([row(ratio=bad)])))) as client:
        with pytest.raises(MarginSourceError, match='有效百分比'):
            source.list_reports(client, DAY, DAY)


def test_null_metric_remains_pending_and_can_be_retried_by_date():
    responses = [payload([row(ratio=None)]), payload([row()])]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=responses.pop(0)))) as client:
        report = source.list_reports(client, DAY, DAY)[0]
        with pytest.raises(MarginSourceError, match='尚未披露'):
            source.fetch_point(client, report)
        point = source.fetch_point(client, source.MarginReport(DAY, report.url))
        assert point['ratio'] == 262.6319


@pytest.mark.parametrize('data', [[], {}, dict(success=False, code=403),
    payload([row()], count=2), payload([row(), row()]),
    payload([row(date(2026, 11, 6))]), payload([dict(STATISTICS_DATE=str(DAY))])])
def test_invalid_response_never_treated_as_complete_history(data):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as client:
        with pytest.raises(MarginSourceError):
            source.list_reports(client, DAY, DAY)


def test_explicit_empty_range_is_not_a_provider_failure():
    data = dict(success=False, code=9201, result=None)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as client:
        assert source.list_reports(client, DAY, DAY) == []


def test_pagination_and_cancellation(monkeypatch):
    monkeypatch.setattr(source, 'PAGE_SIZE', 1)
    calls = []
    def respond(request):
        page = int(request.url.params['pageNumber'])
        calls.append(page)
        return httpx.Response(200, json=payload([row(date(2026, 11, 6-page))], pages=2, count=2))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert len(source.list_reports(client, date(2026, 11, 4), DAY)) == 2
    assert calls == [1, 2]
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def cancelled():
            raise InterruptedError()
        with pytest.raises(InterruptedError):
            source.list_reports(client, DAY, DAY, check_cancel=cancelled)
    assert calls == [1, 2]


def test_http_failures_are_safe_and_do_not_follow_redirects():
    for status in (302, 403, 429, 503):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, text='secret', headers={'Location':'https://evil.test'}))) as client:
            with pytest.raises(MarginSourceError) as error:
                source.list_reports(client, DAY, DAY)
        assert safe_error_details(error.value)['http_status'] == status
        assert 'secret' not in str(error.value)


def test_upgrade_preserves_history_and_reads_one_batch_then_incremental(tmp_path):
    from app.core.config import Settings
    from app.services.margin_guarantee import MarginGuarantee, CHINA
    from datetime import datetime
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload([row(), row(date(2026, 11, 4), 259.3444)]))
    service = MarginGuarantee(Settings(_env_file=None, log_dir=tmp_path, margin_worker_enabled=False),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)), delay=0)
    moment = datetime(2026, 11, 8, 8, tzinfo=CHINA)
    with service.db() as db:
        db.execute('INSERT INTO points VALUES (?,?,?,?)', ('2026-11-04',259.3444,'https://www.csdata.cn/old.xlsx','original'))
    service.write_state(dict(catalog_ready=True))
    service.refresh(background=False, moment=moment)
    assert len(requests) == 1
    assert "2020-11-08" in requests[0].url.params['filter']
    saved = service.history(moment)['points']
    assert len(saved) == 2 and saved[0]['updated_at'] == 'original'
    assert service.status(moment)['error'] is None
    service.refresh(background=False, moment=moment)
    assert len(requests) == 2
    assert "2026-10-29" in requests[1].url.params['filter']
    assert service.history(moment)['points'] == saved


def test_cancelled_backfill_resumes_old_pending_dates_in_one_batch(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.margin_guarantee import MarginGuarantee, CHINA
    from datetime import datetime
    calls = []
    old = date(2021, 1, 4)
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=payload([row(), row(old, 250)]))
    service = MarginGuarantee(Settings(_env_file=None, log_dir=tmp_path, margin_worker_enabled=False),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    moment = datetime(2026, 11, 8, 8, tzinfo=CHINA)
    write = service.write_state
    def cancel_after_one(state, db=None):
        write(state, db)
        if state.get('processed') == 1 and state.get('active'):
            service.cancel()
    monkeypatch.setattr(service, 'write_state', cancel_after_one)
    waits = []
    monkeypatch.setattr(service.stop_event, 'wait', lambda delay: waits.append(delay))
    service.refresh(background=False, moment=moment)
    assert service.status(moment)['pending_reports'] == 1
    assert len(service.history(moment)['points']) == 1
    monkeypatch.setattr(service, 'write_state', write)
    service.refresh(background=False, moment=moment)
    assert len(calls) == 2
    assert '2021-01-04' in calls[-1].url.params['filter']
    assert service.status(moment)['pending_reports'] == 0
    assert len(service.history(moment)['points']) == 2
    assert waits == []
