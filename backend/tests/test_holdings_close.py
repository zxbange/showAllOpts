from datetime import datetime
import json
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from account_helpers import install_accounts, sign_in
from app.api.routes.personal_holdings import router
from app.core.config import Settings
from app.services import holdings_close, holdings_close_source, personal_holdings
from app.services.holdings_snapshot import normalize
from app.services.personal_holdings import PersonalHoldings


MONDAY = '2026-11-23'
FRIDAY = '2026-11-20'
TOKEN = 'test-only-tushare'
ZONE = ZoneInfo('Asia/Shanghai')
CONFIG = dict(host='db.test', port=3306, database='stocks', username='reader',
              password='test-only-secret', table='positions')


def moment(day, at):
    return datetime.fromisoformat(f'{day}T{at}').replace(tzinfo=ZONE)


def row(code='000001', qty='2.5', strategy='A'):
    return dict(code=code, amt=qty, strategy2=strategy, name='模拟证券')


def put_rows(monitor, *rows):
    with monitor.db() as db:
        current = monitor.state(db)
        current.update(baseline=True, rows=normalize(rows), version=current['version'] + 1)
        monitor.write(db, current)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    current = [moment(MONDAY, '15:05')]
    monkeypatch.setattr(personal_holdings, 'clock', lambda: current[0])
    settings = Settings(_env_file=None, log_dir=tmp_path / 'log', auth_allow_http=True, tushare_token=TOKEN)
    monitor = PersonalHoldings(settings)
    monitor.configure(CONFIG)
    monitor.set_enabled(True)
    put_rows(monitor, row())

    def reject_network(*args, **kwargs):
        pytest.fail('Unexpected close source call')

    monkeypatch.setattr(holdings_close, 'fetch_daily_closes', reject_network)
    return monitor, current


def collect(monitor, current, day, at):
    current[0] = moment(day, at)
    return monitor.close_prices.collect_due(current[0])


def test_weekend_early_then_exact_1505(fixture, monkeypatch):
    monitor, current = fixture
    assert not collect(monitor, current, '2026-11-21', '15:05')
    assert not collect(monitor, current, MONDAY, '15:04:59')
    calls = []

    def fetch(day, token):
        calls.append((day.isoformat(), token))
        return {'000001.SZ': '12.34'}

    monkeypatch.setattr(holdings_close, 'fetch_daily_closes', fetch)
    assert collect(monitor, current, MONDAY, '15:05:00')
    assert calls == [(MONDAY, TOKEN)]
    assert monitor.status()['close_valuation'] == {
        'trade_date': MONDAY, 'prices': {'000001': '12.34'}, 'strategy_totals': {'A': '30.85'},
    }


def test_delayed_publication_cooldown_restart_and_previous_close(fixture, monkeypatch):
    monitor, current = fixture
    calls = []

    def fetch(day, token):
        calls.append(day.isoformat())
        if calls == [FRIDAY, MONDAY]:
            raise holdings_close.HoldingsCloseUnavailable()
        return {'000001.SZ': '10' if day.isoformat() == FRIDAY else '12'}

    monkeypatch.setattr(holdings_close, 'fetch_daily_closes', fetch)
    assert collect(monitor, current, FRIDAY, '15:05')
    assert not collect(monitor, current, MONDAY, '15:05')
    restarted = PersonalHoldings(monitor.settings)
    assert restarted.status()['close_valuation'] == {
        'trade_date': FRIDAY, 'prices': {'000001': '10'}, 'strategy_totals': {'A': '25.00'},
    }
    assert not collect(restarted, current, MONDAY, '15:09:59')
    assert collect(restarted, current, MONDAY, '15:10')
    second_restart = PersonalHoldings(monitor.settings)
    assert not collect(second_restart, current, MONDAY, '15:15')
    assert calls == [FRIDAY, MONDAY, MONDAY]
    assert second_restart.status()['close_valuation']['strategy_totals'] == {'A': '30.00'}


def test_api_status_uses_decode_keys_and_current_rows_without_exposing_market(monkeypatch, fixture):
    monitor, current = fixture
    calls = []
    put_rows(monitor, row(), row('600000.XSHG', '3'), row('920001', '1', 'B'))

    def upstream(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            'code': 0, 'data': {
                'fields': ['close', 'ts_code', 'trade_date'],
                'items': [
                    ['12.34', '000001.SZ', '20261123'],
                    ['4.56', '600000.SH', '20261123'],
                    ['2.55', '920001.BJ', '20261123'],
                    ['7.88', '600999.SH', '20261123'],
                ],
            },
        })

    client_type = httpx.Client

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr(holdings_close_source.httpx, 'Client', client)
    monkeypatch.setattr(holdings_close, 'fetch_daily_closes', holdings_close_source.fetch_daily_closes)
    assert collect(monitor, current, MONDAY, '15:05')
    assert calls == [dict(api_name='daily', token=TOKEN, params={'trade_date': '20261123'},
                          fields='ts_code,trade_date,close')]

    app = FastAPI()
    app.state.personal_holdings = monitor
    install_accounts(app, monitor.settings)
    app.include_router(router, prefix='/api/v1')
    with TestClient(app) as session:
        path = '/api/v1/personal-holdings/status'
        # Domain test fixtures log in on TestClient entry. Verify anonymous
        # access independently, then use a real administrator session.
        session.cookies.clear()
        assert session.get(path).status_code == 401
        sign_in(session)
        status = session.get(path)
        assert status.status_code == 200, status.text
        assert status.json()['close_valuation'] == {
            'trade_date': MONDAY,
            'prices': {'000001': '12.34', '600000.XSHG': '4.56', '920001': '2.55'},
            'strategy_totals': {'A': '44.53', 'B': '2.55'},
        }
        assert TOKEN not in status.text
        assert '600999' not in status.text
        put_rows(monitor, row(qty='10'), row('000002', strategy='B'), row('920001', '2', 'B'))
        updated = session.get(path).json()['close_valuation']
        assert updated == {
            'trade_date': MONDAY, 'prices': {'000001': '12.34', '920001': '2.55'},
            'strategy_totals': {'A': '123.40', 'B': None},
        }
    assert len(calls) == 1


def test_switch_source_during_fetch_does_not_publish(fixture, monkeypatch):
    monitor, current = fixture

    def fetch(day, token):
        monitor.configure(dict(CONFIG, table='positions_new'))
        put_rows(monitor, row())
        return {'000001.SZ': '12.34'}

    monkeypatch.setattr(holdings_close, 'fetch_daily_closes', fetch)
    assert not collect(monitor, current, MONDAY, '15:05')
    assert monitor.status()['close_valuation'] == {
        'trade_date': None, 'prices': {}, 'strategy_totals': {'A': None},
    }
    with monitor.db() as db:
        assert db.execute('SELECT count(*) FROM holdings_close_values').fetchone() == (0,)


def test_partial_market_never_replaces_same_day_covered_prices(fixture, monkeypatch):
    monitor, current = fixture
    put_rows(monitor, row(), row('600000', '3'), row('920001', '1', 'B'))
    responses = iter([
        {'000001.SZ': '10', '920001.BJ': '2'},
        {'000001.SZ': '10', '600000.SH': '4'},
        {'000001.SZ': '10', '600000.SH': '4', '920001.BJ': '2'},
    ])
    monkeypatch.setattr(holdings_close, 'fetch_daily_closes', lambda day, token: next(responses))
    assert collect(monitor, current, MONDAY, '15:05')
    first = monitor.status()['close_valuation']
    assert first == {
        'trade_date': MONDAY, 'prices': {'000001': '10', '920001': '2'},
        'strategy_totals': {'A': None, 'B': '2.00'},
    }
    assert not collect(monitor, current, MONDAY, '15:09:59')
    assert not collect(monitor, current, MONDAY, '15:10')
    assert monitor.status()['close_valuation'] == first
    assert collect(monitor, current, MONDAY, '15:15')
    assert monitor.status()['close_valuation'] == {
        'trade_date': MONDAY, 'prices': {'000001': '10', '600000': '4', '920001': '2'},
        'strategy_totals': {'A': '37.00', 'B': '2.00'},
    }
