from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.models.microcap import DEFAULT_SERIES, MicrocapTemperature
from app.repositories.microcap import MicrocapRepository
from app.services.momentum import china_today


def row(day, score, index=1000, series=DEFAULT_SERIES, growth=None):
    return MicrocapTemperature(
        series_id=series, trade_date=day, micro_num=1000, sample_step=5, sample_count=20,
        universe_count=3000, profit_growth_count=0 if growth is None else 950,
        micro_median=15, all_median=25, relative_ratio=.6, crowding_ratio=1.5,
        amount_crowding_ratio=.2, pe_median=-5, pb_median=2, avg_np_growth=growth,
        cap_pct=.3, relative_pct=.4, crowding_pct=.5, amount_crowding_pct=.6,
        score=Decimal(str(score)), micro_index=index, period_return=.05,
        member_codes='["private-constituent"]', updated_at=datetime(2026, 1, 6, 17),
    )


def test_empty_and_catalog(client):
    response = client.get('/api/v1/microcap-temperature/history')
    assert response.status_code == 200
    assert response.json()['latest'] is None and response.json()['points'] == []
    assert client.get('/api/v1/modules').json()['modules'][-1]['id'] == 'microcap-temperature'


def test_history_units_scope_nulls_and_fixed_percentiles(client, session):
    session.add_all([
        row(date(2026, 1, 6), .6, 1100, growth=-20),
        row(date(2026, 1, 5), .4),
        row(date(2026, 1, 6), .99, series='other-series'),
        row(china_today() + timedelta(days=1), .99),
    ])
    session.commit()
    response = client.get('/api/v1/microcap-temperature/history?days=0')
    assert response.status_code == 200
    data = response.json()
    assert [point['score'] for point in data['points']] == [.4, .6]
    assert [point['avg_np_growth'] for point in data['points']] == [None, -20]
    assert data['latest']['pe_median'] == -5
    assert data['latest']['micro_index'] == 1100
    assert data['summary']['score_change'] == pytest.approx(20)
    assert data['summary']['index_change_pct'] == pytest.approx(10)
    assert 'member_codes' not in response.text
    historical = client.get('/api/v1/microcap-temperature/history?end_date=2026-01-05').json()
    assert historical['latest']['score'] == .4
    assert historical['summary']['score_change'] == 0


def test_date_boundary_and_window_does_not_recompute_ranks(client, session):
    end = date(2026, 1, 6)
    start = end - timedelta(days=29)
    session.add_all([row(start - timedelta(days=1), .2), row(start, .3), row(end, .7)])
    session.commit()
    full = client.get('/api/v1/microcap-temperature/history?days=0').json()
    short = client.get('/api/v1/microcap-temperature/history?days=30').json()
    assert short['points'] == full['points'][1:]
    assert short['start_date'] == start.isoformat()
    assert short['summary']['observed_samples'] == 2


def test_single_select_does_not_read_constituents(client, engine, session):
    session.add(row(date(2026, 1, 6), .4))
    session.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert client.get('/api/v1/microcap-temperature/history').status_code == 200
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert len(statements) == 1 and statements[0].lstrip().startswith('SELECT')
    assert 'member_codes' not in statements[0]


@pytest.mark.parametrize('query', ['days=1', 'days=-1', 'days=366', 'days=no', 'end_date=bad'])
def test_invalid_query(client, query):
    assert client.get('/api/v1/microcap-temperature/history?' + query).status_code == 422


def test_future_end_and_database_error(client, monkeypatch):
    assert client.get('/api/v1/microcap-temperature/history?end_date=' + str(china_today()+timedelta(days=1))).status_code == 422
    def fail(*args):
        raise OperationalError('SELECT secret', {}, Exception('password=private-password'))
    monkeypatch.setattr(MicrocapRepository, 'history', fail)
    response = client.get('/api/v1/microcap-temperature/history')
    assert response.status_code == 503 and 'private-password' not in response.text
