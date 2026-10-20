from datetime import date, datetime
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from app.models.growth import GrowthSnapshot, GrowthStock
from app.repositories.growth import GrowthRepository


def reports():
    return {name: [dict(report_period=f'{year}-{md}', publication_date=f'{year+1}-02-01' if name=='annual' else f'{year}-11-01',
                       net_profit=str(100000000 * (1.2 ** i)), eps=str(1.2 ** i),
                       profit_growth=None if i==0 else '20', eps_growth=None if i==0 else '20')
                  for i,year in enumerate([2023,2024,2025])] for name,md in [('annual','12-31'),('q1','03-31'),('q2','06-30'),('q3','09-30')]}


def seed(session, empty=False):
    session.add(GrowthSnapshot(id=1,as_of_date=date(2026,10,20),valuation_date=date(2026,10,19),updated_at=datetime(2026,10,20,10),
                              rule_version='growth_cumulative_parent_eps_v1', universe_count=5000,evaluated_count=4000,matched_count=0 if empty else 1))
    if not empty:
        session.add(GrowthStock(code='000001.XSHE',as_of_date=date(2026,10,20),rank=1,name='测试高增长',industry_name='测试行业',
                               pe_ttm=20,pb=2,market_cap=100,annual_profit_growth=20,reports=reports()))
    session.commit()


def test_uncollected_and_valid_empty_differ(client,session):
    response=client.get('/api/v1/high-growth/latest')
    assert response.status_code==200 and response.json()==dict(snapshot=None,items=[])
    seed(session,True)
    data=client.get('/api/v1/high-growth/latest').json()
    assert data['snapshot']['matched_count']==0 and data['items']==[]


def test_latest_evidence_and_readonly_single_query(client,session,engine):
    seed(session)
    statements=[]
    def capture(conn,cursor,sql,*args):statements.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    try:response=client.get('/api/v1/high-growth/latest')
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert response.status_code==200
    stock=response.json()['items'][0]
    assert stock['annual_profit_growth']==20 and stock['pe_ttm']==20
    assert len(stock['reports'])==4 and all(len(r)==3 for r in stock['reports'].values())
    assert stock['reports']['q2'][0]['report_period']=='2023-06-30'
    assert stock['reports']['annual'][0]['net_profit']==100000000
    assert len(statements)==3 and statements[-1].lstrip().startswith('SELECT')
    assert client.post('/api/v1/high-growth/latest').status_code==405


def test_database_exception_sanitized(client,monkeypatch):
    def fail(*args):raise OperationalError('SELECT',{},Exception('password=secret'))
    monkeypatch.setattr(GrowthRepository,'latest',fail)
    response=client.get('/api/v1/high-growth/latest')
    assert response.status_code==503 and 'secret' not in response.text
    assert 'high-growth' in [m['id'] for m in client.get('/api/v1/modules').json()['modules']]
