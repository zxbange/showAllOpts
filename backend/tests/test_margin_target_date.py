"""Scheduled/statistics cutoff and late disclosure against the actual JSON parser."""

from datetime import datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.services.collection_outcomes import latest_outcomes, outcome_history
from app.services.margin_guarantee import CHINA, MarginGuarantee

RUN = datetime(2026, 11, 26, 8, tzinfo=CHINA)


@pytest.mark.parametrize('null_row', [False, True])
def test_scheduled_missing_target_is_not_success_and_next_day_backfills(tmp_path, null_row, caplog):
    caplog.set_level('INFO', logger='app.services.margin_guarantee')
    values = {'2026-11-24': 250.0}
    if null_row:
        values['2026-11-25'] = None
    requests = []

    def source(request):
        params = request.url.params
        requests.append(params['filter'])
        end = params['filter'].split("<='")[-1][:10]
        start = params['filter'].split(">='")[-1][:10]
        rows = [
            {'STATISTICS_DATE': day + ' 00:00:00', 'AVG_GUARANTEE_RATIO': values[day]}
            for day in sorted(values, reverse=True)
            if start <= day <= end
        ]
        return httpx.Response(
            200,
            json={
                'success': True,
                'code': 0,
                'result': {'data': rows, 'pages': 1, 'count': len(rows)},
            },
        )

    settings = Settings(
        _env_file=None,
        log_dir=tmp_path,
        margin_worker_enabled=False,
        cache_enabled=False,
        social_worker_enabled=False,
    )
    monitor = MarginGuarantee(
        settings,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(source)),
        delay=0,
    )
    try:
        assert monitor.refresh(background=False, moment=RUN, slot=RUN.isoformat())
        assert requests[-1].endswith("(STATISTICS_DATE<='2026-11-25')")
        history = monitor.history(RUN)
        assert history['latest']['date'] == '2026-11-24'
        status = history['status']
        assert status['target_date'] == '2026-11-25'
        assert status['last_success_at'] is None
        assert '2026-11-25' in status['error']
        assert status['pending_reports'] == int(null_row)
        assert (
            'Margin observation saved report_date=2026-11-24 statistics_date=2026-11-24 inserted=1'
            in caplog.text
        )
        assert 'Margin target coverage target=2026-11-25 saved=False' in caplog.text
        assert 'target_report=' + ('ratio_null' if null_row else 'missing') in caplog.text
        outcome = outcome_history(settings, 'margin-guarantee')['items'][0]
        assert outcome['outcome'] == 'partial'
        assert outcome['error_details']['code'] == 'margin_target_not_ready'
        assert outcome['error_details']['trade_date'] == '2026-11-25'
        previous = history['points'][0]

        # Same slot remains deduplicated. Next normal slot sees the late date
        # alongside the new eligible date, while excluding 27's early data.
        assert not monitor.refresh(background=False, moment=RUN, slot=RUN.isoformat())
        values.update({'2026-11-25': 251.0, '2026-11-26': 252.0, '2026-11-27': 253.0})
        next_run = RUN + timedelta(days=1)
        assert monitor.refresh(background=False, moment=next_run, slot=next_run.isoformat())
        assert requests[-1].endswith("(STATISTICS_DATE<='2026-11-26')")
        history = monitor.history(next_run)
        assert [point['date'] for point in history['points']] == [
            '2026-11-24',
            '2026-11-25',
            '2026-11-26',
        ]
        assert history['points'][0] == previous
        assert (
            'Margin observation saved report_date=2026-11-25 statistics_date=2026-11-25 inserted=1'
            in caplog.text
        )
        assert (
            'Margin observation saved report_date=2026-11-26 statistics_date=2026-11-26 inserted=1'
            in caplog.text
        )
        assert 'Margin target coverage target=2026-11-26 saved=True' in caplog.text
        assert history['status']['error'] is None
        assert history['status']['pending_reports'] == 0
        assert latest_outcomes(settings)['items']['margin-guarantee']['outcome'] == 'success'
    finally:
        monitor.stop()
