import pytest
from datetime import date, datetime
from app.models.etf_heat import Heat, Pool


def seed(session):
    members=[dict(code=c,name=c,category='宽基ETF',size_yi='40',size_date='2026-10-20',size_source='test',target_name=c,fund_type='ETF') for c in ['A','B','C']]
    session.add(Pool(id=1,as_of_date=date(2026,10,20),rule_version='etf_close_momentum_v1',members=members,updated_at=datetime(2026,10,20,17)))
    for day, code, score in [(19,'A',1),(19,'B',2),(20,'A',3),(20,'B',-1),(20,'C',None),(20,'OLD',100)]:
        session.add(Heat(trade_date=date(2026,10,day),code=code,name=code,score=score,close=2,
                         reference_pass=None if score is None else 0, details={'status':'ok'},updated_at=datetime(2026,10,20)))
    session.commit()


def test_latest_current_pool_rank_negative_null_and_change(client,session):
    assert client.get('/api/v1/etf-momentum/latest').json()['trade_date'] is None
    seed(session)
    data=client.get('/api/v1/etf-momentum/latest').json()
    assert [i['code'] for i in data['items']]==['A','B','C']
    assert [i['rank'] for i in data['items']]==[1,2,None]
    assert data['items'][0]['rank_change']==1
    assert data['items'][1]['score']==-1
    assert data['items'][2]['score'] is None


def test_history_missing_points_and_retired_funds(client,session):
    seed(session)
    result=client.get('/api/v1/etf-momentum/funds/C/history').json()
    assert result['points'][0]['score'] is None
    assert result['points'][0]['details']['status']=='missing'
    result=client.get('/api/v1/etf-momentum/funds/OLD/history').json()
    assert result['fund'] is None and result['points'][-1]['score']==100
    assert client.get('/api/v1/etf-momentum/funds/NONE/history').status_code==404
    assert client.get('/api/v1/etf-momentum/funds/A/history?days=9999').status_code==422


@pytest.mark.parametrize('version', ['etf_close_momentum_v1', 'etf_close_momentum_v3'])
def test_cache_incremental_and_disconnected_source(engine,session,tmp_path,version):
    from fastapi.testclient import TestClient
    from sqlalchemy import event
    from app.core.cache import SnapshotCache
    from app.core.config import Settings
    from app.main import create_app
    seed(session)
    session.get(Pool,1).rule_version=version;session.commit()
    settings=Settings(_env_file=None,cache_enabled=True,cache_auto_refresh=False,log_dir=tmp_path/'log')
    cache=SnapshotCache(settings,engine);cache.refresh('etf-momentum',background=False)
    previous=cache.read_state()['modules']['etf-momentum']['file']
    head=session.get(Pool,1);head.updated_at=datetime(2026,10,21);head.members=head.members[:1];session.commit()
    cache.refresh('etf-momentum',background=False)
    assert cache.read_state()['modules']['etf-momentum']['file'] != previous
    def no_remote(*args):raise AssertionError('source queried by page')
    event.listen(engine,'before_cursor_execute',no_remote)
    try:
        with TestClient(create_app(settings,engine)) as local:
            response=local.get('/api/v1/etf-momentum/latest')
            assert response.status_code==200 and response.headers['x-data-source']=='local-cache'
            assert len(response.json()['items'])==1
            assert local.get('/api/v1/etf-momentum/funds/A/history').status_code==200
    finally:event.remove(engine,'before_cursor_execute',no_remote)


def test_raw_version_and_incomplete_result_preserve_published_cache(engine, session, tmp_path):
    from app.core.cache import SnapshotCache
    from app.core.config import Settings
    seed(session)
    cache=SnapshotCache(Settings(_env_file=None,cache_enabled=True,cache_auto_refresh=False,log_dir=tmp_path/'log'),engine)
    cache.refresh('etf-momentum',background=False)
    before=cache.read_state()['modules']['etf-momentum']['file']
    head=session.get(Pool,1)
    head.rule_version='etf_raw_market_v1';head.updated_at=datetime(2026,10,21);session.commit()
    cache.refresh('etf-momentum',background=False)
    assert cache.read_state()['modules']['etf-momentum']['file']==before
    assert next(m for m in cache.status()['modules'] if m['id']=='etf-momentum')['error']
    head.rule_version='etf_close_momentum_v2';head.members=head.members+[{'code':'MISSING'}]
    head.updated_at=datetime(2026,10,22);session.commit()
    cache.refresh('etf-momentum',background=False)
    assert cache.read_state()['modules']['etf-momentum']['file']==before


def test_direct_access_rejects_raw_pool_without_calculating(client, session):
    seed(session)
    session.get(Pool,1).rule_version='etf_raw_market_v1';session.commit()
    result=client.get('/api/v1/etf-momentum/latest')
    assert result.status_code==503


def test_cache_ignores_old_raw_tables_and_preserves_source_scores(engine, session, tmp_path):
    from sqlalchemy import event, text
    from app.core.cache import SnapshotCache
    from app.core.config import Settings
    seed(session)
    with engine.begin() as c:
        c.execute(text('CREATE TABLE etf_momentum_bar (ignored TEXT)'))
        c.execute(text('CREATE TABLE raw_industry_momentum (ignored TEXT)'))
    statements=[]
    def capture(c,cur,sql,*args):statements.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    try:
        cache=SnapshotCache(Settings(_env_file=None,cache_enabled=True,cache_auto_refresh=False,log_dir=tmp_path/'log'),engine)
        cache.refresh(background=False)
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert all(m['ready'] and not m['error'] for m in cache.status()['modules'])
    assert all('etf_momentum_bar' not in s and 'raw_industry_momentum' not in s for s in statements)
    with cache.session('etf-momentum') as local:
        assert local.get(Heat,(date(2026,10,20),'A')).score==3
