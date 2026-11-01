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
    session.flush()
    session.get(EarningsRun,period).matched_count = session.query(EarningsStock).filter_by(report_period=period).count()
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
    assert 'analysis' not in data['items'][0] and 'analysis_basis' not in data['items'][0]
    assert len(data['industries'])==2
    result=client.get('/api/v1/earnings-gap/stocks?period=2026-09-30&industry=test&search=测试&source=formal').json()
    assert result['total']==1 and result['items'][0]['pb']==0 and result['items'][0]['pe_ttm']==-5
    assert client.get('/api/v1/earnings-gap/stocks?period=2026-09-30&search=%25').json()['total']==0
    detail=client.get('/api/v1/earnings-gap/stocks/600001.XSHG?period=2026-09-30').json()
    assert detail['events'][0]['raw']['original']==123 and len(detail['prices'])==1
    assert 'analysis' not in detail and 'analysis_basis' not in detail
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
    assert all(q.lstrip().startswith(('SELECT', 'PRAGMA')) for q in statements)
    assert len(statements) <= 9  # Fixed batch enrichment, never per-stock queries.
    listing = next(q for q in statements if 'earnings_gap_stock.name' in q)
    assert 'earnings_gap_stock.events' not in listing
    assert 'earnings_gap_stock.prices' not in listing
    assert 'LIMIT' in listing
    assert 'earnings_gap_stock.analysis' not in listing


def test_returns_repeated_quarters_and_latest_close_with_legacy_compatibility(client, session):
    from app.models.earnings import EarningsPerformance
    seed(session, intraday_pct=5)
    seed(session, period=date(2026, 6, 30))
    url = '/api/v1/earnings-gap/stocks?period=2026-09-30'
    item = client.get(url).json()['items'][0]
    assert item['close_change_pct'] == pytest.approx(9.2)
    assert item['selected_periods'] == ['2026-09-30', '2026-06-30']
    assert item['since_signal_pct'] is None and item['latest_price_date'] is None
    session.add(EarningsPerformance(report_period=P, code=item['code'], signal_date=date(2026, 10, 15),
        as_of_date=date(2026, 10, 30), latest_date=date(2026, 10, 29), signal_close=10,
        latest_close=9, since_signal_pct=-10, updated_at=datetime(2026, 10, 30, 17)))
    session.commit()
    item = client.get(url).json()['items'][0]
    assert item['since_signal_pct'] == -10 and item['latest_price_date'] == '2026-10-29'
    detail = client.get('/api/v1/earnings-gap/stocks/600001.XSHG?period=2026-09-30').json()
    assert detail['selected_periods'] == item['selected_periods']
    assert detail['since_signal_pct'] == -10
    session.query(EarningsPerformance).update({'signal_date': date(2026, 10, 14)})
    session.commit()
    assert client.get(url).json()['items'][0]['since_signal_pct'] is None


def test_legacy_cache_without_performance_table_still_renders(client, session, engine):
    from app.models.earnings import EarningsPerformance
    seed(session)
    EarningsPerformance.__table__.drop(engine)
    data = client.get('/api/v1/earnings-gap/stocks?period=2026-09-30').json()
    assert data['items'][0]['close_change_pct'] == pytest.approx(4)
    assert data['items'][0]['since_signal_pct'] is None
