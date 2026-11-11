"""Collection diagnostics describe actual outcomes without upstream secrets."""
import logging

import pytest

from test_cash_cow import Source, screen, run
from test_growth_dividend import monitor as growth_monitor, provider as growth_provider, MOMENT as GROWTH_MOMENT
from test_margin_guarantee import monitor as margin_monitor, provider as margin_provider, MOMENT as MARGIN_MOMENT
from app.services import growth_dividend, margin_guarantee

SECRET = 'token=DO_NOT_LOG password=hidden https://secret.example/private'


def assert_finished(caplog, label, outcome):
    messages = [r.getMessage() for r in caplog.records if label + ' collection finished' in r.getMessage()]
    assert len(messages) == 1
    assert f'outcome={outcome}' in messages[0]
    assert 'duration_seconds=' in messages[0]
    assert 'processed=' in messages[0]
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize('outcome', ['success', 'failed', 'cancelled'])
def test_cash_cow_outcome(screen, caplog, outcome):
    caplog.set_level(logging.INFO)
    class CustomSource(Source):
        def universe(self):
            if outcome == 'failed':
                raise RuntimeError(SECRET)
            if outcome == 'cancelled':
                screen.cancel()
                self.check_cancel()
            return super().universe()
    screen.source_factory = CustomSource
    run(screen)
    assert_finished(caplog, 'Cash cow', outcome)
    assert '测试股票' not in caplog.text


@pytest.mark.parametrize('outcome', ['success', 'partial', 'failed', 'cancelled'])
def test_growth_outcome(growth_monitor, monkeypatch, caplog, outcome):
    caplog.set_level(logging.INFO)
    growth_provider(monkeypatch)
    if outcome != 'success':
        def fail(*args, **kwargs):
            if outcome == 'cancelled':
                raise growth_dividend.CollectionCancelled()
            if outcome == 'partial':
                raise ValueError(SECRET)
            raise RuntimeError(SECRET)
        monkeypatch.setattr(growth_dividend, 'fetch_calendar', fail)
    growth_monitor.refresh(background=False, moment=GROWTH_MOMENT)
    assert_finished(caplog, 'Growth/dividend', outcome)


@pytest.mark.parametrize('outcome', ['success', 'partial', 'failed', 'cancelled'])
def test_margin_outcome(margin_monitor, monkeypatch, caplog, outcome):
    caplog.set_level(logging.INFO)
    margin_provider(monkeypatch, [MARGIN_MOMENT.date()])
    if outcome != 'success':
        def fail(*args, **kwargs):
            if outcome == 'cancelled':
                raise margin_guarantee.CollectionCancelled()
            raise RuntimeError(SECRET)
        monkeypatch.setattr(margin_guarantee, 'fetch_point' if outcome == 'partial' else 'list_reports', fail)
    margin_monitor.refresh(background=False, moment=MARGIN_MOMENT)
    assert_finished(caplog, 'Margin guarantee', outcome)


def test_growth_market_closed_outcome(growth_monitor, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    growth_provider(monkeypatch, days=[])
    growth_monitor.refresh(background=False, moment=GROWTH_MOMENT, slot='test-closed')
    assert_finished(caplog, 'Growth/dividend', 'market_closed')


def test_growth_finalize_failure_does_not_claim_success(growth_monitor, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    growth_provider(monkeypatch)
    original = growth_monitor.write_state
    def write(state, db=None):
        if not state.get('active'):
            raise OSError(SECRET)
        return original(state, db)
    monkeypatch.setattr(growth_monitor, 'write_state', write)
    with pytest.raises(OSError):
        growth_monitor.refresh(background=False, moment=GROWTH_MOMENT)
    assert_finished(caplog, 'Growth/dividend', 'failed')
    assert not growth_monitor.running()
