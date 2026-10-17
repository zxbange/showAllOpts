from datetime import date, datetime
import pytest
from sqlalchemy import event
from app.models.earnings import EarningsRun, EarningsStock
from app.services.earnings import latest_period

P=date(2026,9,30)


def seed(session, period=P, code='600001.XSHG', **values):
    if session.get(EarningsRun,period) is None:
        session.add(EarningsRun(report_period=period,as_of_date=date(2026,10,16),
            updated_at=datetime(2026,10,17),universe_count=6000,matched_count=2,rule_version='announcement-open-v1'))
    row=dict(report_period=period,code=code,name='测试公司',industry_code='test',
        industry_name='测试行业',industry_level='sw_l3',signal_date=date(2026,10,15),
        announcement_date=date(2026,10,15),signal_source='formal',profit_basis='单季度',
        profit_yoy=25,net_profit=1e8,open_gap_pct=4,intraday_pct=0,market_cap=50,
        circulating_market_cap=40,pe_ttm=-5,pb=0,valuation_date=date(2026,10,16),
        analysis='测试分析。'*45,analysis_basis='量价与财务数据',events=[{'kind':'formal','raw':{'original':123}}],
        prices=[{'date':'2026-10-15','open':10.4,'close':10.5,'low':10,'high':10.6}])
    row.update(values)
    session.add(EarningsStock(**row))
    session.commit()


def test_empty_latest_does_not_fall_back(client,session,monkeypatch):
    monkeypatch.setattr('app.services.earnings.latest_period',lambda:P)
    seed(session,period=date(2026,6,30))
    data=client.get('/api/v1/earnings-gap/stocks').json()
    assert data['target_period']==str(P) and data['run'] is None and data['items']==[]
    assert data['periods']==['2026-06-30']


def test_list_filter_sort_page_detail_and_nulls(client,session):
    seed(session)
    seed(session,code='000001.XSHE',name='第二公司',profit_yoy=70,market_cap=None,pe_ttm=None,industry_code='other',industry_name='其他')
    data=client.get('/api/v1/earnings-gap/stocks?period=2026-09-30&sort=profit_yoy&page_size=1').json()
    assert data['total']==2 and data['items'][0]['code']=='000001.XSHE'
    assert data['items'][0]['market_cap'] is None
    assert 'events' not in data['items'][0] and 'prices' not in data['items'][0]
    assert len(data['industries'])==2
    result=client.get('/api/v1/earnings-gap/stocks?period=2026-09-30&industry=test&search=测试&source=formal').json()
    assert result['total']==1 and result['items'][0]['pb']==0 and result['items'][0]['pe_ttm']==-5
    assert client.get('/api/v1/earnings-gap/stocks?period=2026-09-30&search=%25').json()['total']==0
    detail=client.get('/api/v1/earnings-gap/stocks/600001.XSHG?period=2026-09-30').json()
    assert detail['events'][0]['raw']['original']==123 and len(detail['prices'])==1
    assert client.get('/api/v1/earnings-gap/stocks/none?period=2026-09-30').status_code==404


@pytest.mark.parametrize('query',['sort=analysis','source=bad','direction=bad','page=0','page_size=10000',
    'period=2026-08-01','period=2099-12-31','period=bad'])
def test_invalid_query(client,query):
    assert client.get('/api/v1/earnings-gap/stocks?'+query).status_code==422


def test_read_only_queries_and_no_large_payload_in_listing(client,session,engine):
    seed(session)
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(engine,'before_cursor_execute',capture)
    try:
        response=client.get('/api/v1/earnings-gap/stocks?period=2026-09-30')
    finally: event.remove(engine,'before_cursor_execute',capture)
    assert response.status_code==200
    assert all(q.lstrip().startswith('SELECT') for q in statements)
    assert len(statements)==5
    assert 'earnings_gap_stock.events' not in statements[-1]
    assert 'earnings_gap_stock.prices' not in statements[-1]
    assert 'LIMIT' in statements[-1]
