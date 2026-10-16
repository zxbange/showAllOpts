from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.models.momentum import IndustryMomentum as Row
from app.repositories.momentum import MomentumRepository
from app.services.momentum import china_today


def row(day, code, value, kind='sw_l2', name=None):
    return Row(trade_date=day, industry_type=kind, industry_code=code,
               industry_name=name or f'行业{code}', momentum=Decimal(str(value)),
               momentum_rank=99, front_stock_count=0 if value == 0 else 2,
               industry_stock_count=10, updated_at=datetime(2026, 1, 6, 17, 0))


def test_latest_snapshot_is_sorted_includes_zero_and_filters_type(client, session):
    session.add_all([
        row(date(2026, 1, 5), 'A', 2), row(date(2026, 1, 5), 'B', 0),
        row(date(2026, 1, 6), 'A', 3), row(date(2026, 1, 6), 'B', 0),
        row(date(2026, 1, 6), 'C', 3), row(date(2026, 1, 7), 'X', 90, 'sw_l1'),
        row(china_today() + timedelta(days=1), 'A', 999),
    ])
    session.commit()
    response = client.get('/api/v1/industry-momentum/latest')
    assert response.status_code == 200
    result = response.json()
    assert result['trade_date'] == '2026-01-06'
    assert result['previous_trade_date'] == '2026-01-05'
    assert [i['code'] for i in result['industries']] == ['C', 'A', 'B']
    assert [i['rank'] for i in result['industries']] == [1, 2, 3]
    assert result['industries'][0]['change'] is None
    assert result['industries'][1]['change'] == 1
    assert result['industries'][2]['momentum'] == 0
    assert result['summary'] == {'industry_count': 3, 'active_count': 2,
                                  'average_momentum': 2, 'max_momentum': 3}


def test_empty_database_has_explicit_empty_state(client):
    result = client.get('/api/v1/industry-momentum/latest').json()
    assert result['trade_date'] is None
    assert result['industries'] == []


def test_history_is_chronological_and_missing_is_null_not_zero(client, session):
    session.add_all([
        row(date(2026, 1, 5), 'A', 2), row(date(2026, 1, 6), 'B', 1),
        row(date(2026, 1, 7), 'A', 0), row(date(2026, 1, 8), 'A', 5),
    ])
    session.commit()
    result = client.get('/api/v1/industry-momentum/industries/A/history').json()
    assert [p['date'] for p in result['points']] == ['2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08']
    assert [p['momentum'] for p in result['points']] == [2, None, 0, 5]
    assert result['summary']['missing_days'] == 1
    assert result['summary']['observed_days'] == 3
    assert result['summary']['change'] == 3
    assert result['summary']['peak_date'] == '2026-01-08'


def test_one_year_window_includes_365_calendar_days_and_preserves_snapshot_anchor(client, session):
    end = date(2026, 1, 8)
    start = end - timedelta(days=364)
    session.add_all([row(start - timedelta(days=1), 'A', 9), row(start, 'A', 2),
                     row(end, 'A', 3), row(end + timedelta(days=1), 'A', 7)])
    session.commit()
    result = client.get(f'/api/v1/industry-momentum/industries/A/history?end_date={end}').json()
    assert result['start_date'] == start.isoformat()
    assert result['end_date'] == end.isoformat()
    assert [p['momentum'] for p in result['points']] == [2, 3]


def test_existing_industry_with_empty_window_returns_empty_points(client, session):
    session.add_all([row(date(2025, 1, 2), 'A', 2), row(date(2026, 1, 6), 'B', 4)])
    session.commit()
    result = client.get('/api/v1/industry-momentum/industries/A/history?days=30').json()
    assert result['summary']['observed_days'] == 0
    assert result['summary']['latest_momentum'] is None
    assert all(point['momentum'] is None for point in result['points'])


def test_unknown_industry_and_invalid_query(client):
    assert client.get('/api/v1/industry-momentum/industries/unknown/history').status_code == 404
    assert client.get('/api/v1/industry-momentum/latest?industry_type=unknown').status_code == 422
    assert client.get('/api/v1/industry-momentum/industries/A/history?days=0').status_code == 422
    tomorrow = china_today() + timedelta(days=1)
    assert client.get(f'/api/v1/industry-momentum/industries/A/history?end_date={tomorrow}').status_code == 422


def test_database_failure_does_not_expose_connection_secrets(client, monkeypatch, tmp_path):
    def fail(*args):
        raise OperationalError('SELECT secret', {}, Exception('password=secret-password'))
    monkeypatch.setattr(MomentumRepository, 'latest_date', fail)
    response = client.get('/api/v1/industry-momentum/latest')
    assert response.status_code == 503
    assert response.json()['code'] == 'DATABASE_UNAVAILABLE'
    assert 'secret-password' not in response.text
    error_log = (tmp_path / 'log' / 'error.log').read_text(encoding='utf-8')
    assert 'OperationalError' in error_log and 'Traceback' in error_log
    assert 'secret-password' not in error_log


def test_api_only_executes_selects(client, engine, session):
    session.add(row(date(2026, 1, 6), 'A', 2))
    session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert client.get('/api/v1/industry-momentum/latest').status_code == 200
        assert client.get('/api/v1/industry-momentum/industries/A/history').status_code == 200
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert statements and set(statements) == {'SELECT'}


def test_module_catalog_and_health(client):
    assert client.get('/api/v1/health').json()['status'] == 'ok'
    assert client.get('/api/v1/modules').json()['modules'][0]['id'] == 'industry-momentum'
