from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.models.etf import ETFShare
from app.repositories.etf import ETFRepository
from app.services.momentum import china_today


def row(day, shares, code='510300.XSHG', previous=None):
    return ETFShare(fund_code=code, fund_name='stored-name', trade_date=day, shares=shares,
                    previous_trade_date=previous or day-timedelta(days=1),
                    source='jqdata.FUND_SHARE_DAILY', updated_at=datetime(2026, 1, 6, 18))


def test_empty_catalog(client):
    result = client.get('/api/v1/etf-shares/history').json()
    assert result['end_date'] is None and result['observed_days'] == 0
    assert len(result['funds']) == 5 and all(f['shares'] is None for f in result['funds'])
    assert 'etf-shares' in {module['id'] for module in client.get('/api/v1/modules').json()['modules']}


def test_latest_units_previous_trading_day_stale_and_scope(client, session):
    friday, monday = date(2026, 1, 2), date(2026, 1, 5)
    session.add_all([row(friday, 100), row(monday, 125, previous=friday),
                    row(friday, 0, '510310.XSHG'), row(monday, 99, '510500.XSHG'),
                    row(china_today()+timedelta(days=1), 999)])
    session.commit()
    data = client.get('/api/v1/etf-shares/history?days=0').json()
    fund, stale, missing = data['funds'][:3]
    assert fund['shares'] == '125' and fund['shares_change'] == '25'
    assert fund['daily_change_pct'] == 25 and fund['previous_date'] == str(friday)
    assert fund['name'] == '沪深300ETF华泰柏瑞'
    assert stale['shares'] == '0' and stale['lagging'] and stale['date'] == str(friday)
    assert stale['daily_change_pct'] is None and missing['shares'] is None
    assert data['baseline_date'] is None  # No positive common baseline with the zero-only fund.
    assert client.get('/api/v1/etf-shares/history?group=zz500').json()['funds'][0]['shares'] == '99'
    assert client.get('/api/v1/etf-shares/history?end_date=2026-01-02').json()['funds'][0]['shares'] == '100'


def test_missing_previous_day_is_not_multi_day_change_and_bigint_is_string(client, session):
    session.add_all([row(date(2026, 1, 2), 1), row(date(2026, 1, 6), 9007199254740993)])
    session.commit()
    fund = client.get('/api/v1/etf-shares/history').json()['funds'][0]
    assert fund['shares'] == '9007199254740993'
    assert fund['shares_change'] is None and fund['daily_change_pct'] is None


def test_aligned_common_baseline_no_pre_baseline_returns(client, session):
    d1, d2, d3 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)
    session.add_all([row(d1, 100), row(d2, 200), row(d3, 300),
                    row(d2, 10, '510310.XSHG'), row(d3, 0, '510310.XSHG')])
    session.commit()
    data = client.get('/api/v1/etf-shares/history').json()
    assert data['baseline_date'] == str(d2) and data['available_funds'] == 2
    assert [p['change_pct'] for p in data['funds'][0]['points']] == [None, 0, 50]
    assert [p['change_pct'] for p in data['funds'][1]['points']] == [0, -100]


def test_zero_previous_denominator_has_absolute_delta_but_no_pct(client, session):
    session.add_all([row(date(2026, 1, 5), 0), row(date(2026, 1, 6), 100)])
    session.commit()
    fund = client.get('/api/v1/etf-shares/history').json()['funds'][0]
    assert fund['shares_change'] == '100' and fund['daily_change_pct'] is None


def test_range_anchored_at_latest_and_two_read_only_queries(client, session, engine):
    end = date(2026, 1, 6)
    start = end-timedelta(days=29)
    session.add_all([row(start-timedelta(days=1), 50), row(start, 100), row(end, 150)])
    session.commit()
    statements=[]
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        response = client.get('/api/v1/etf-shares/history?days=30')
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    data=response.json()
    assert data['start_date'] == str(start) and len(data['funds'][0]['points']) == 2
    assert len(statements) == 2 and all(s.lstrip().startswith('SELECT') for s in statements)
    assert 'updated_at' not in statements[1]
    assert 'trade_date >=' in statements[1] and 'fund_code IN' in statements[1]


@pytest.mark.parametrize('query', ['group=no', 'days=1', 'days=-1', 'days=366', 'days=no', 'end_date=bad'])
def test_invalid_query(client, query):
    assert client.get('/api/v1/etf-shares/history?' + query).status_code == 422


def test_future_and_db_failure(client, monkeypatch):
    assert client.get('/api/v1/etf-shares/history?end_date='+str(china_today()+timedelta(days=1))).status_code == 422
    def fail(*args):
        raise OperationalError('SELECT secret', {}, Exception('private-password'))
    monkeypatch.setattr(ETFRepository, 'latest', fail)
    response = client.get('/api/v1/etf-shares/history')
    assert response.status_code == 503 and 'private-password' not in response.text
