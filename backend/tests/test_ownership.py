from test_cache import cached
from datetime import date, datetime
from app.models.ownership import OwnershipStock, OwnershipReport, OwnershipHolders
from app.services.ownership import latest_quarter

def test_api_search_detail_and_social(client,session):
    stamp=datetime(2026,10,21)
    session.add(OwnershipStock(code='600001.XSHG',name='示例公司',industry='行业',updated_at=stamp))
    session.add(OwnershipReport(code='600001.XSHG',report_date=date(2026,9,30),payload={'categories':{},'float_pub_date':'2026-10-20','social_entry':{'status':'comparable','first_appearance':True,'new_combinations':[{'name':'社保一零一','shares':100}],'new_market_value':200}},updated_at=stamp))
    for end,pub,a,total in [(date(2026,6,30),None,100,200),(date(2026,9,30),date(2026,10,20),80,180),(date(2099,9,30),date(2099,10,20),1,1)]:
        session.add(OwnershipHolders(code='600001.XSHG',end_date=end,pub_date=pub,payload={'a_share_holders':a,'share_holders':total},updated_at=stamp))
    session.commit()
    assert client.get('/api/v1/institution-ownership/stocks?q=示例').json()['stocks'][0]['code']=='600001.XSHG'
    assert client.get('/api/v1/institution-ownership/stocks?q=%25').json()['stocks']==[]
    detail=client.get('/api/v1/institution-ownership/stocks/600001').json()
    assert detail['holder_change']==-20 and detail['holder_change_pct']==-20
    assert len(detail['holders'])==2
    result=client.get('/api/v1/institution-ownership/social-entries?period=2026-09-30').json()
    assert len(result['items'])==1 and result['comparable_count']==1
    assert client.get('/api/v1/institution-ownership/stocks/invalid').status_code==422
    assert client.get('/api/v1/institution-ownership/stocks/999999').status_code==404
    assert client.get('/api/v1/institution-ownership/social-entries?period=2026-09-29').status_code==422
    assert client.get('/api/v1/institution-ownership/social-entries?mode=wrong').status_code==422

def test_latest_quarter_does_not_fall_back():
    assert latest_quarter(date(2026,10,21))==date(2026,9,30)
    assert latest_quarter(date(2026,1,1))==date(2025,12,31)

def test_json_incremental_cache_updates_and_preserves_null(cached,session):
    from test_cache_incremental import assert_matches
    _,cache=cached
    stamp=datetime(2026,10,21)
    session.add(OwnershipStock(code='600001.XSHG',name='示例',industry=None,updated_at=stamp))
    report=OwnershipReport(code='600001.XSHG',report_date=date(2026,9,30),payload={'categories':{'social':None}},updated_at=stamp)
    session.add(report);session.commit()
    cache.refresh('institution-ownership',background=False)
    assert_matches(cache,session,'institution-ownership')
    report.payload={'categories':{'social':{'shares':100}},'social_entry':{'status':'unknown'}}
    report.updated_at=datetime(2026,10,21,1);session.commit()
    cache.refresh('institution-ownership',background=False)
    assert_matches(cache,session,'institution-ownership')
    session.delete(report);session.commit()
    cache.refresh('institution-ownership',background=False)
    assert_matches(cache,session,'institution-ownership')
