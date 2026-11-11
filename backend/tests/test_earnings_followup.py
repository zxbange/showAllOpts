"""Daily returns use one adjusted basis, remain resumable, and expire with quarters."""
import datetime as dt
from types import SimpleNamespace
import pandas as pd
import pytest
from sqlalchemy import create_engine, select, event
from app.core.earnings_sync import sync_performance
from app.models.earnings import EarningsPerformance
from test_earnings_collector import c, record


def save(c, period, signal, code='600001.XSHG'):
    item = record(c, period, code)
    item.update(signal_date=signal, announcement_date=signal)
    c.env['save_results'](c.Session, period, item['valuation_date'], 6000, [item])


def refresh(c, day, fetch):
    return c.env['_refresh_earnings_followup'](c.Session, c.env['EarningsStock'], day, fetch)


def rows(c):
    with c.engine.connect() as db:
        return [dict(r) for r in db.execute(select(EarningsPerformance.__table__)).mappings()]


def test_multiple_quarters_share_quotes_and_repeat_skips_then_updates(c):
    save(c, dt.date(2026, 6, 30), dt.date(2026, 10, 14))
    save(c, dt.date(2026, 9, 30), dt.date(2026, 10, 15))
    calls = []
    def fetch(code, **kw):
        calls.append((code, kw))
        return pd.DataFrame({'close': [5, 10, 9], 'paused': [0, 0, 0]},
                            index=pd.to_datetime(['2026-10-14', '2026-10-15', '2026-10-16']))
    assert refresh(c, dt.date(2026, 10, 16), fetch) == 2
    assert len(calls) == 1 and calls[0][1]['fq'] == 'pre'
    assert [float(r['since_signal_pct']) for r in rows(c)] == [80, -10]
    assert refresh(c, dt.date(2026, 10, 16), lambda *a, **kw: pytest.fail('already current')) == 0
    # A later split changes both endpoints in the same new response; no old snapshot is used.
    def split(code, **kw):
        return pd.DataFrame({'close': [2.5, 5, 6], 'paused': [0, 0, 0]},
                            index=pd.to_datetime(['2026-10-14', '2026-10-15', '2026-10-19']))
    refresh(c, dt.date(2026, 10, 19), split)
    assert [float(r['since_signal_pct']) for r in rows(c)] == [140, 20]
    assert refresh(c, dt.date(2026, 10, 16), lambda *a, **kw: pytest.fail('date regression')) == 0


def test_suspension_shows_actual_price_date_and_missing_signal_preserves_old(c):
    save(c, dt.date(2026, 9, 30), dt.date(2026, 10, 15))
    def fetch(code, **kw):
        return pd.DataFrame({'close': [10, 10], 'paused': [0, 1]},
                            index=pd.to_datetime(['2026-10-15', '2026-10-16']))
    refresh(c, dt.date(2026, 10, 16), fetch)
    old = rows(c)
    assert old[0]['latest_date'] == dt.date(2026, 10, 15)
    assert old[0]['since_signal_pct'] == 0
    with pytest.raises(ValueError):
        refresh(c, dt.date(2026, 10, 19), lambda *a, **kw: pd.DataFrame(
            {'close': [12], 'paused': [0]}, index=pd.to_datetime(['2026-10-19'])))
    assert rows(c) == old


def test_history_retention_preserves_old_quarters_and_sync_prices(c, tmp_path):
    cutoff = dt.date(2021, 9, 30)
    for period in (dt.date(2021, 6, 30), cutoff, dt.date(2026, 6, 30)):
        save(c, period, dt.date(2026, 9, 29))
    refresh(c, dt.date(2026, 9, 30), lambda *a, **kw: pd.DataFrame(
        {'close': [10, 12], 'paused': [0, 0]}, index=pd.to_datetime(['2026-09-29', '2026-09-30'])))
    target = create_engine('sqlite:///' + str(tmp_path / 'target.sqlite'))
    try:
        with c.engine.connect() as source, target.begin() as local:
            sync_performance(source, local)
        # Seed run date is October 16; use it explicitly as the rolling cutoff reference.
        with c.Session() as db:
            db.query(c.env['EarningsRun']).update({'as_of_date': dt.date(2026, 9, 30)})
            db.commit()
        prune = c.env['_prune_earnings_history']
        assert prune(c.Session, c.env['EarningsStock'], c.env['EarningsRun'], dt.date(2026, 9, 30)) == 0
        assert prune(c.Session, c.env['EarningsStock'], c.env['EarningsRun'], dt.date(2026, 9, 30)) == 0
        with c.engine.connect() as source, target.begin() as local:
            sync_performance(source, local)
            assert [r[0] for r in local.execute(select(EarningsPerformance.report_period).order_by(EarningsPerformance.report_period))] == [dt.date(2021, 6, 30), cutoff, dt.date(2026, 6, 30)]
        with c.Session() as db:
            assert db.query(c.env['EarningsStock']).count() == 3
            assert db.query(c.env['EarningsRun']).count() == 3
            assert {r[0] for r in db.execute(c.env['_earnings_version_table']().select())} == {dt.date(2021, 6, 30), cutoff, dt.date(2026, 6, 30)}
    finally:
        target.dispose()


def test_followup_schema_matches_collector(c):
    table = c.env['_earnings_followup_table']()
    assert [(x.name, str(x.type), x.primary_key, x.nullable) for x in table.columns] == [
        (x.name, str(x.type), x.primary_key, x.nullable) for x in EarningsPerformance.__table__.columns]


def test_followup_write_failure_rolls_back_all_quarters(c):
    save(c, dt.date(2026, 6, 30), dt.date(2026, 10, 15))
    save(c, dt.date(2026, 9, 30), dt.date(2026, 10, 15))
    inserts = []
    def fail(conn, cursor, statement, *args):
        if statement.startswith('INSERT INTO earnings_gap_performance'):
            inserts.append(statement)
            if len(inserts) == 2:
                raise RuntimeError('write failed')
    event.listen(c.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError):
            refresh(c, dt.date(2026, 10, 16), lambda *a, **kw: pd.DataFrame(
                {'close': [10], 'paused': [0]}, index=pd.to_datetime(['2026-10-15'])))
    finally:
        event.remove(c.engine, 'before_cursor_execute', fail)
    assert rows(c) == []
