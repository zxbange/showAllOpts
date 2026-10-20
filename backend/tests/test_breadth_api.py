from datetime import date, datetime, timedelta
import math
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from app.models.breadth import MicrocapBreadth, DEFAULT_SERIES
from app.repositories.breadth import BreadthRepository
from app.services.momentum import china_today


def row(day, high=200, low=100, series=DEFAULT_SERIES):
    return MicrocapBreadth(series_id=series,trade_date=day,sample_count=400,
                          **{f'{side}_{p}':v for p in (5,10,20,30) for side,v in [('high',high),('low',low)]},
                          member_codes='private-constituents',updated_at=datetime(2026,10,16,17))


def test_empty_and_catalog(client):
    response=client.get('/api/v1/microcap-breadth/history')
    assert response.status_code==200 and response.json()['latest'] is None
    assert 'microcap-breadth' in [m['id'] for m in client.get('/api/v1/modules').json()['modules']]


def test_units_scope_weighting_and_range_invariance(client,session):
    start=date(2026,8,1)
    session.add_all([row(start+timedelta(days=i),high=100+i,low=80) for i in range(50)])
    session.add_all([row(start,series='other'),row(china_today()+timedelta(days=1))]);session.commit()
    full=client.get('/api/v1/microcap-breadth/history?days=90').json()
    short=client.get('/api/v1/microcap-breadth/history?days=30').json()
    assert short['points']==full['points'][-30:]
    latest=short['latest'];assert latest['strength']==pytest.approx(69/4)
    assert latest['sentiment']==pytest.approx(69/4*sum(math.exp(-p/20) for p in (5,10,20,30)))
    assert latest['change_6d']==pytest.approx(1.5)
    assert latest['change_12d']==pytest.approx(3)
    assert latest['periods'][0]['dominance_pct']==100
    assert latest['periods'][0]['observed_days']==15
    assert full['points'][0]['change_6d'] is None
    assert 'member_codes' not in str(full)
    historical=client.get('/api/v1/microcap-breadth/history?end_date='+str(start)).json()
    assert historical['latest']['date']==str(start) and historical['observed_days']==1


def test_query_is_bounded_read_only_and_excludes_constituents(client,engine,session):
    session.add(row(date(2026,10,16)));session.commit()
    statements=[]
    def capture(conn,cursor,statement,*args): statements.append(statement)
    event.listen(engine,'before_cursor_execute',capture)
    try: assert client.get('/api/v1/microcap-breadth/history').status_code==200
    finally: event.remove(engine,'before_cursor_execute',capture)
    assert len(statements)==1 and statements[0].lstrip().startswith('SELECT')
    assert 'LIMIT' in statements[0] and 'member_codes' not in statements[0]


@pytest.mark.parametrize('query',['days=0','days=1','days=301','days=oops','end_date=invalid'])
def test_bad_query(client,query):
    assert client.get('/api/v1/microcap-breadth/history?'+query).status_code==422


def test_future_and_database_failure(client,monkeypatch):
    assert client.get('/api/v1/microcap-breadth/history?end_date='+str(china_today()+timedelta(days=1))).status_code==422
    def fail(*args): raise OperationalError('SELECT',{},Exception('password=secret'))
    monkeypatch.setattr(BreadthRepository,'history',fail)
    response=client.get('/api/v1/microcap-breadth/history')
    assert response.status_code==503 and 'secret' not in response.text
