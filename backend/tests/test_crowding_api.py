from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.models.crowding import MarketCrowding
from app.repositories.crowding import CrowdingRepository
from app.services.momentum import china_today


def row(day, ratio, top_pct='0.05'):
    return MarketCrowding(
        top_pct=Decimal(top_pct), trade_date=day, ratio=Decimal(str(ratio)),
        top_money=Decimal(str(ratio)) * 1000, total_money=1000,
        stock_count=5000, top_stock_count=250, updated_at=datetime(2026, 1, 6, 17),
    )


def test_empty_and_module_catalog(client):
    data = client.get('/api/v1/market-crowding/history').json()
    assert data['latest'] is None and data['points'] == []
    assert data['summary']['observed_days'] == 0
    assert [module['id'] for module in client.get('/api/v1/modules').json()['modules']] == [
        'industry-momentum', 'market-crowding', 'market-sentiment', 'microcap-temperature', 'earnings-gap', 'etf-shares', 'microcap-breadth', 'dividend-selection', 'high-growth', 'etf-momentum', 'institution-ownership', 'short-term']


def test_scope_order_statistics_and_percentage_points(client, session):
    session.add_all([
        row(date(2026, 1, 6), .4), row(date(2026, 1, 5), .2),
        row(date(2026, 1, 6), .9, '.1'),
        row(date(2019, 12, 31), .9),
        row(china_today() + timedelta(days=1), .9),
    ])
    session.commit()
    response = client.get('/api/v1/market-crowding/history?days=0')
    assert response.status_code == 200
    data = response.json()
    assert [p['ratio'] for p in data['points']] == [.2, .4]
    assert data['latest']['ratio'] == .4
    assert data['latest']['top_stock_count'] == 250
    assert data['summary']['change_pp'] == pytest.approx(20)
    assert data['summary']['average_ratio'] == pytest.approx(.3)
    assert data['summary']['peak_ratio'] == .4
    assert data['summary']['low_ratio'] == .2
    assert data['points'][1]['smoothed_ratio'] == pytest.approx((.4 + .2 * 9/11) / (1 + 9/11))


def test_window_boundary_and_smoothing_are_stable(client, session):
    end = date(2026, 1, 6)
    start = end - timedelta(days=29)
    session.add_all([row(start - timedelta(days=1), .2), row(start, .3), row(end, .4)])
    session.commit()
    full = client.get('/api/v1/market-crowding/history?days=0').json()
    short = client.get('/api/v1/market-crowding/history?days=30').json()
    assert short['start_date'] == start.isoformat()
    assert short['points'] == full['points'][1:]
    assert short['summary']['observed_days'] == 2
    historical = client.get(f'/api/v1/market-crowding/history?days=0&end_date={start}').json()
    assert historical['latest']['date'] == start.isoformat()
    assert historical['points'] == full['points'][:2]


@pytest.mark.parametrize('query', ['days=-1', 'days=1', 'days=366', 'days=no', 'end_date=bad'])
def test_invalid_query(client, query):
    assert client.get('/api/v1/market-crowding/history?' + query).status_code == 422


def test_future_end_date(client):
    tomorrow = china_today() + timedelta(days=1)
    assert client.get(f'/api/v1/market-crowding/history?end_date={tomorrow}').status_code == 422


def test_one_read_only_query(client, engine, session):
    session.add(row(date(2026, 1, 6), .35))
    session.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        response = client.get('/api/v1/market-crowding/history?days=365')
        assert response.status_code == 200
        assert response.json()['summary']['change_pp'] == 0
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert statements == ['SELECT']


def test_error_redaction(client, monkeypatch):
    def fail(*args):
        raise OperationalError('SELECT secret', {}, Exception('password=private-password'))
    monkeypatch.setattr(CrowdingRepository, 'history', fail)
    response = client.get('/api/v1/market-crowding/history')
    assert response.status_code == 503
    assert 'private-password' not in response.text
