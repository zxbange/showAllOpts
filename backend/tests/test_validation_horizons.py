"""Custom holding periods must survive resume and retain exact market-day targets."""
import json

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.api.routes.short_term import ValidationRequest
from app.services import short_statistics as stats
from app.services.short_validation import ValidationJobs
from test_short_validation import setup, finish


@pytest.mark.parametrize('horizons', [[], [0], [121], [1, 2, 3, 4], [True], [1.5], ['2']])
def test_invalid_horizons_rejected_at_api_and_service(setup, horizons):
    jobs, _, _, calendar = setup
    with pytest.raises(ValidationError):
        ValidationRequest(methods=['oversold'], horizons=horizons)
    with pytest.raises(ValueError, match='验证周期'):
        jobs.create(['oversold'], calendar[170].date(), horizons=horizons)
    assert jobs.list() == []


def test_horizons_normalized_and_part_of_task_identity(setup):
    jobs, _, _, calendar = setup
    start = calendar[170].date()
    first = jobs.create(['oversold'], start, horizons=[120, 1, 7], request_id='same')
    assert first['horizons'] == [1, 7, 120]
    assert jobs.create(['oversold'], start, horizons=[7, 120, 1], request_id='same')['id'] == first['id']
    assert jobs.create(['oversold'], start, horizons=[7], request_id='same')['id'] != first['id']
    with pytest.raises(ValueError, match='验证周期'):
        jobs.create(['oversold'], start, horizons=[2, 2])


def test_120th_market_day_and_missing_or_suspended_target_do_not_roll():
    days = pd.bdate_range('2025-01-01', periods=122)
    frame = pd.DataFrame({'close': np.arange(100, 222), 'adj_close': np.arange(100, 222), 'paused': 0}, index=days)
    result = stats.forward_outcomes(frame, [1, 7, 120])
    assert result.iloc[0].d120_date == days[120]
    assert result.iloc[0].d120_return_pct == pytest.approx(120)
    assert result.iloc[2].d120_status == '待观察'
    frame.loc[days[7], 'paused'] = 1
    frame.loc[days[120], 'close'] = np.nan
    result = stats.forward_outcomes(frame, [7, 120])
    assert result.iloc[0].d7_status == '停牌'
    assert result.iloc[0].d120_status == '缺少行情'
    assert pd.isna(result.iloc[0].d7_return_pct)
    assert pd.isna(result.iloc[0].d120_return_pct)


@pytest.mark.parametrize('mode', ['frozen', 'static-batch-v1'])
def test_custom_periods_persist_and_resume_with_correct_denominators(setup, mode):
    jobs, settings, engine, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), calendar[-1].date(),
                      horizons=[1, 7, 120], input_mode=mode)
    jobs.tick()  # Commit the plan, then simulate a restart.
    resumed = ValidationJobs(settings, engine)
    result = finish(resumed, job['id'])
    assert result['status'] == 'completed', result
    assert result['horizons'] == [1, 7, 120]
    assert result['windows'] == []  # Arbitrary periods are not a three-day window.
    rows = {row['后续交易日']: row for row in result['summary']}
    assert set(rows) == {1, 7, 120}
    assert rows[7]['有效样本'] > 0
    assert rows[7]['平均收益(%)'] == pytest.approx((10 / 9.5 - 1) * 100)
    assert rows[120]['有效样本'] == 0
    assert rows[120]['待观察'] == rows[120]['触发样本'] > 0
    assert rows[120]['胜率(%)'] is None
    with resumed.db() as db:
        assert {r[0] for r in db.execute('SELECT DISTINCT horizon FROM outcomes')} == {1, 7, 120}


def test_legacy_job_without_horizons_keeps_123(setup):
    jobs, _, _, calendar = setup
    job = jobs.create(['oversold'], calendar[170].date(), calendar[195].date())
    with jobs.db() as db:
        stored = jobs.get(job['id'], db)
        stored.pop('horizons')
        stored['requested'].pop('horizons')
        jobs.save(db, stored)
    result = finish(jobs, job['id'])
    assert result['status'] == 'completed', json.dumps(result)
    assert [r['后续交易日'] for r in result['summary']] == [1, 2, 3]
    assert result['windows'][0]['完整三日样本'] > 0


def test_local_statistics_entry_uses_custom_periods_in_all_exports(setup, tmp_path):
    _, _, engine, calendar = setup
    result = stats.run(start_date=calendar[170], end_date=calendar[-1], as_of=calendar[-1],
                       engine=engine, output_dir=tmp_path / 'reports', show_progress=False,
                       strategies=['超跌后止跌反弹'], horizons=[120, 7])
    assert result['summary']['后续交易日'].tolist() == [7, 120]
    assert result['windows'].empty
    destination = result['output_dir']
    assert not (destination / '三日窗口汇总.csv').exists()
    assert set(pd.read_csv(destination / '按月收盘胜率.csv')['后续交易日']) == {7, 120}
    assert json.loads((destination / '运行参数.json').read_text(encoding='utf-8'))['horizons'] == [7, 120]
    assert 'd120_return_pct' in result['details'] and 'd1_return_pct' not in result['details']


def test_local_statistics_cli_forwards_periods(monkeypatch):
    calls = []
    monkeypatch.setattr(stats, 'run', lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(stats, 'load_local_config', lambda *_: {
        'DB_CONFIG': {}, 'DB_TLS_VERIFY': True, 'DB_TLS_CA_PEM': '',
    })
    assert stats.main(['--horizons', '7', '120']) == 0
    assert calls[0]['horizons'] == [7, 120]
