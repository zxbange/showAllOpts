from datetime import date, datetime
import json

import pytest

from app.core.config import Settings
from app.services import earnings_schedule, index_valuation_source, valuation_ruler
from app.services.cash_cow import CashCowScreen
from app.services.dividend_collection import DividendCollection, DividendCollectionError


def at(ts):
    return datetime.fromisoformat(ts + '+08:00')


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, log_dir=tmp_path)


@pytest.mark.parametrize('ts, cutoff', [
    ('2026-11-24T16:00:00', '2026-11-23'),
    ('2026-11-24T22:59:59', '2026-11-23'),
    ('2026-11-28T23:00:00', '2026-11-27'),
])
def test_current_sources_do_not_fetch_today_or_guess_yesterday(settings, monkeypatch, ts, cutoff):
    monkeypatch.setattr(earnings_schedule, 'task_config',
                        lambda *_: {'times': ['23:00'], 'enabled': False})
    dividend = DividendCollection(settings)
    cash = CashCowScreen(settings)
    for service, exc in ((dividend, DividendCollectionError), (cash, earnings_schedule.EarningsNotDue)):
        def forbidden(*args, **kwargs):
            pytest.fail('A fresh collection was dispatched early')
        monkeypatch.setattr(service, '_collect', forbidden)
        before = service.read_state()
        with pytest.raises(exc, match=cutoff):
            service.refresh(background=False, moment=at(ts))
        assert service.read_state() == before
        assert not service.running()


@pytest.mark.parametrize('collection', [DividendCollection, CashCowScreen])
def test_exact_configured_due_time_manual_even_when_disabled(settings, monkeypatch, collection):
    monkeypatch.setattr(earnings_schedule, 'task_config',
                        lambda *_: {'times': ['21:15'], 'enabled': False})
    service = collection(settings)
    called = []

    def collected(lock, state, moment):
        called.append(moment.date())
        lock.release()

    monkeypatch.setattr(service, '_collect', collected)
    assert service.refresh(background=False, moment=at('2026-11-24T21:15:00'))
    assert called == [date(2026, 11, 24)]


@pytest.mark.parametrize('collection, error', [
    (DividendCollection, DividendCollectionError),
    (CashCowScreen, earnings_schedule.EarningsNotDue),
])
def test_configured_early_time_respects_current_source_floor(settings, monkeypatch, collection, error):
    monkeypatch.setattr(earnings_schedule, 'task_config',
                        lambda *_: {'times': ['12:00'], 'enabled': False})
    service = collection(settings)
    called = []

    def collect(lock, state, moment):
        called.append(moment.date())
        lock.release()

    monkeypatch.setattr(service, '_collect', collect)
    with pytest.raises(error, match='2026-11-23'):
        service.refresh(background=False, moment=at('2026-11-24T15:59:59'))
    assert not called and not service.running()
    assert service.refresh(background=False, moment=at('2026-11-24T16:00:00'))
    assert called == [date(2026, 11, 24)]


def test_valuation_before_schedule_requires_exact_archive(settings, monkeypatch):
    monitor = valuation_ruler.ValuationRuler(settings)
    monkeypatch.setattr(valuation_ruler, 'task_config',
                        lambda *_: {'times': ['18:30'], 'enabled': False})
    moment = at('2026-11-24T18:29:59')
    with pytest.raises(valuation_ruler.ValuationNotDue, match='2026-11-23'):
        monitor.refresh(background=False, moment=moment)
    with monitor.db() as db:
        db.execute('CREATE TABLE index_rosters_v2 (trade_date TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT INTO index_rosters_v2 VALUES (?,?)', ('2026-11-23', '{}'))
    called = []

    def collected(state, check, current, *, cutoff, automatic):
        called.append(cutoff)

    monkeypatch.setattr(monitor, '_collect', collected)
    assert monitor.refresh(background=False, moment=moment)
    assert monitor.refresh(background=False, moment=at('2026-11-24T18:30:00'))
    assert called == [date(2026, 11, 23), date(2026, 11, 24)]


def test_historical_valuation_uses_archived_not_live_roster(settings, monkeypatch):
    monitor = valuation_ruler.ValuationRuler(settings)
    cutoff = date(2026, 11, 23)
    today = date(2026, 11, 24)
    codes = tuple(index_valuation_source.INDEXES)
    rosters = {code: ['600000.XSHG'] for code in codes}
    with monitor.db() as db:
        db.execute('CREATE TABLE index_rosters_v2 (trade_date TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.executemany('INSERT INTO index_rosters_v2 VALUES (?,?)',
                       [(str(d), json.dumps(rosters)) for d in (cutoff, today)])
    source = index_valuation_source.EastmoneySource(None, monitor, lambda: None)
    close_calls, value_calls = [], []

    def closes(code, day):
        close_calls.append((code, day))
        return {day: 10}

    def values(symbols, day):
        value_calls.append((symbols, day))
        return {'600000.XSHG': {}}

    def forbidden(*args):
        pytest.fail('Live constituents used for older cutoff')

    monkeypatch.setattr(source, 'snapshot', forbidden)
    monkeypatch.setattr(source, 'closes', closes)
    monkeypatch.setattr(source, 'valuations', values)
    monkeypatch.setattr(index_valuation_source, 'summarize', lambda day, *a, **k: day)
    assert source.collect_latest(cutoff, at('2026-11-24T18:29:59')) == [cutoff] * len(codes)
    assert close_calls == [(code, cutoff) for code in codes]
    assert value_calls == [({'600000.XSHG'}, cutoff)]


@pytest.mark.parametrize('pending_day,accepted', [(23, True), (24, False)])
@pytest.mark.parametrize('kind', ['dividend','cash'])
def test_early_recovery_requires_pending_payload_on_or_before_due_day(settings, monkeypatch, pending_day, accepted, kind):
    moment = at('2026-11-24T22:59:00')
    fetched = []
    if kind == 'dividend':
        service = DividendCollection(settings)
        service.stage('test', {'trade_date': date(2026, 11, pending_day)}, [])
        exc = DividendCollectionError
    else:
        service = CashCowScreen(settings)
        payload = json.dumps({'3': {'snapshot': {'as_of': f'2026-11-{pending_day}'}}})
        with service.db() as db:
            db.execute('CREATE TABLE public_publication (id INTEGER PRIMARY KEY,target TEXT,payload TEXT)')
            db.execute('INSERT INTO public_publication VALUES (1,?,?)', ('test', payload))
        exc = earnings_schedule.EarningsNotDue

    def recover(lock, state, moment):
        fetched.append(state)
        lock.release()

    monkeypatch.setattr(service, '_collect', recover)
    if accepted:
        assert service.refresh(background=False, moment=moment)
        assert len(fetched) == 1
    else:
        with pytest.raises(exc, match='2026-11-23'):
            service.refresh(background=False, moment=moment)
        assert not fetched
    assert not service.running()
