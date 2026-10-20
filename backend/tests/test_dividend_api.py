from datetime import date, datetime
from decimal import Decimal
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from app.models.dividend import DividendSnapshot, DividendStock
from app.repositories.dividend import DividendRepository
from test_dividend_collector import row, meta


def seed(session,empty=False):
    data=meta()
    if empty:data.update(classic_count=0,value_count=0,classic_candidates=0,value_candidates=0)
    session.add(DividendSnapshot(id=1,updated_at=datetime(2026,10,16,17),**data))
    if not empty:session.add_all([DividendStock(**row()),DividendStock(**row('value'))])
    session.commit()


def test_empty_uncollected_and_collected_zero_are_distinct(client,session):
    data=client.get('/api/v1/dividend-selection/latest').json()
    assert data['snapshot'] is None and len(data['groups'])==2
    seed(session,empty=True)
    data=client.get('/api/v1/dividend-selection/latest').json()
    assert data['snapshot']['trade_date']=='2026-10-16' and all(not g['items'] for g in data['groups'])


def test_latest_order_units_overlap_and_single_read_only_query(client,session,engine):
    seed(session)
    session.add(DividendStock(**row(code='000002.XSHE',rank=2,dividend_ratio_3y=Decimal('15'))));session.commit()
    statements=[]
    def capture(conn,cursor,sql,*args):statements.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    try:response=client.get('/api/v1/dividend-selection/latest')
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert response.status_code==200
    data=response.json()
    assert [r['rank'] for r in data['groups'][0]['items']]==[1,2]
    assert data['groups'][0]['items'][0]['dividend_yield_1y']==5
    assert data['overlap_codes']==['000001.XSHE']
    assert len(statements)==1 and statements[0].lstrip().startswith('SELECT')
    assert client.post('/api/v1/dividend-selection/latest').status_code==405


def test_error_is_sanitized_and_module_registered(client,monkeypatch):
    def fail(*args):raise OperationalError('SELECT',{},Exception('password=secret'))
    monkeypatch.setattr(DividendRepository,'latest',fail)
    response=client.get('/api/v1/dividend-selection/latest')
    assert response.status_code==503 and 'secret' not in response.text
    assert 'dividend-selection' in [m['id'] for m in client.get('/api/v1/modules').json()['modules']]
