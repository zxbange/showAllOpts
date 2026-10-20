"""Raw source snapshots, pure calculations, recovery and local-only API reads."""
import ast, copy, datetime as dt, importlib.util, sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
import pandas as pd
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.core.raw_inputs import encode_input, decode_input, RAW_TABLES, RAW_VERSION
from app.models.raw import RAW_MODELS
from app.services.raw_calculation import calculate_input
from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.main import create_app
from app.models.momentum import IndustryMomentum
from app.models.earnings import EarningsStock
from test_growth_collector import source as growth_reports
DAY=dt.date(2026,10,16)
STAMP=dt.datetime(2026,10,16,17)
ROOT=Path(__file__).resolve().parents[2]

def cases():
    dates=list(pd.bdate_range(end=DAY,periods=21).date)
    momentum=dict(industries=pd.DataFrame({'name':['行业']},index=['I']),members={'I':['A','B']},window=dates,
        prices=pd.DataFrame([dict(code=c,time=d,close=p) for c in ['A','B'] for d,p in [(dates[0],1),(DAY,1.3 if c=='A' else 1.1)]]))
    codes=['A','B','C']
    micro=dict(rows={c:dict(market_cap=i+1,pe_ratio=6,pb_ratio=1,turnover_ratio=i+1) for i,c in enumerate(codes)},turnovers={c:[Decimal(i+1)]*5 for i,c in enumerate(codes)},amounts={c:Decimal(10*(i+1)) for i,c in enumerate(codes)},growth={c:{'inc_net_profit_to_shareholders_year_on_year':10*(i+1)} for i,c in enumerate(codes)},endpoints={})
    calendar=list(pd.bdate_range(end=DAY,periods=31).date)
    breadth=dict(codes=['A','B'],calendar=calendar,prices=pd.DataFrame([dict(code=c,time=d,close=i+1 if c=='A' else 31-i) for c in ['A','B'] for i,d in enumerate(calendar)]))
    codes=['{:06d}'.format(i) for i in range(100)]
    dividend=dict(names={c:c for c in codes},quotes={c:10 for c in codes},caps={c:dict(market_cap=100,pe_ratio=7,pb_ratio=1) for c in codes},distributions=[dict(id=i,code=c,a_registration_date=DAY,a_xr_date=DAY,bonus_amount_rmb=50000) for i,c in enumerate(codes)],fundamentals={c:dict(inc_total_revenue_year_on_year=10,inc_net_profit_year_on_year=20,inc_return=8,statDate=dt.date(2026,6,30),pubDate=dt.date(2026,8,1)) for c in codes},industries={c:{} for c in codes})
    growth=dict(as_of=DAY,names={'A':'A','B':'B'},reports={'A':growth_reports(),'B':growth_reports(profit=(100,119,150))},caps={c:dict(pe_ratio=10,pb_ratio=1,market_cap=100) for c in ['A','B']},industries={'A':{},'B':{}})
    calendar=[dt.date(2026,10,i) for i in [14,15,16]]
    earnings=dict(names={'A':'A'},period=dt.date(2026,9,30),events=[dict(code='A',kind='formal',report_period='2026-09-30',publication_date='2026-10-15',net_profit=100000000,profit_yoy=25,profit_basis='单季度',eligible=True,signal=None)],calendars={'A':calendar},prices={'A':pd.DataFrame([dict(open=10 if i==0 else 10.4,close=10 if i==0 else 10.4,high=11,low=9,paused=0,volume=100) for i in range(3)],index=calendar)},caps={'A':dict(market_cap=100,circulating_market_cap=90,pe_ratio=7,pb_ratio=1)},industries={'A':{}})
    return {'行业动量':({'industry_type':'sw_l2'},momentum,'sw_l2'),'抱团拥挤度':({'top_pct':.05},{'pairs':[(str(i),i+1) for i in range(100)]},'0.050000'),'微盘温度计':({'micro_num':2,'sample_step':5},micro,'micro2_5d_20210101_v1'),'微盘新高新低':({'micro_num':2},breadth,'micro2_hl_v1'),'高股息选股':({},dividend,'default'),'高增长':({},growth,'default'),'净利润断层':({},earnings,'2026-09-30')}

def payload(name):
    settings,data,scope=cases()[name]
    return dict(strategy=name,day=DAY,scope=scope,settings=settings,inputs=data)

def seed_raw(session,name,value=None):
    value=value or payload(name);model=RAW_MODELS[RAW_TABLES[name][0]];digest,parts=encode_input(value)
    session.add_all(model(scope=value['scope'],trade_date=value['day'],part=i,parts=len(parts),digest=digest,version=RAW_VERSION,payload=p,updated_at=STAMP) for i,p in enumerate(parts));session.commit()
    return model

@pytest.mark.parametrize('name',list(RAW_TABLES))
def test_roundtrip_and_formula(name):
    p=payload(name);digest,parts=encode_input(p)
    rows=[SimpleNamespace(scope=p['scope'],trade_date=DAY,part=i,parts=len(parts),digest=digest,version=RAW_VERSION,payload=v) for i,v in enumerate(parts)]
    decoded=decode_input(rows);assert encode_input(decoded)==(digest,parts)
    out=calculate_input(decoded,STAMP,{})
    if name=='行业动量':assert out[0][1][0]['momentum']==Decimal('.50')
    elif name=='抱团拥挤度':assert out[0][1][0]['ratio']==(Decimal(490)/5050).quantize(Decimal('.0000000001'))
    elif name=='微盘温度计':assert out[0][1][0]['score']==1 and out[0][1][0]['micro_index']==1000
    elif name=='微盘新高新低':assert out[0][1][0]['high_30']==1 and out[0][1][0]['low_5']==1
    elif name=='高股息选股':assert len(out[1][1])==35 and out[1][1][0]['dividend_yield_1y']==5
    elif name=='高增长':assert len(out[1][1])==1 and out[1][1][0]['annual_profit_growth']==20
    elif name=='净利润断层':assert out[1][1][0]['open_gap_pct']==4

def test_microcap_recursive_index():
    p=payload('微盘温度计');state={};first=calculate_input(p,STAMP,state)[0][1][0]
    second=copy.deepcopy(p);second['day']=DAY+dt.timedelta(days=7)
    second['inputs']['endpoints']={(c,d):Decimal(v) for c in ['A','B'] for d,v in [(DAY,10),(second['day'],11)]}
    row=calculate_input(second,STAMP,state)[0][1][0]
    assert first['sample_count']==1 and row['sample_count']==2 and row['micro_index']==1100 and row['score']==Decimal('.75')

def test_corrupt_and_incomplete_parts():
    import random
    digest,parts=encode_input({'data':random.Random(1).randbytes(150000).hex()});assert len(parts)>1
    rows=[SimpleNamespace(scope='x',trade_date=DAY,part=i,parts=len(parts),digest=digest,version=RAW_VERSION,payload=v) for i,v in enumerate(parts)]
    with pytest.raises(ValueError,match='不完整'):decode_input(rows[:-1])
    rows[0].payload=b'bad'
    with pytest.raises(ValueError,match='校验失败'):decode_input(rows)

def settings(tmp_path):return Settings(_env_file=None,cache_enabled=True,cache_auto_refresh=False,log_dir=tmp_path/'log')

def test_all_modules_sync_then_offline_pages(engine,session,tmp_path):
    for name in RAW_TABLES:seed_raw(session,name)
    config=settings(tmp_path);cache=SnapshotCache(config,engine);statements=[]
    def observe(c,cur,sql,*args):statements.append(sql)
    event.listen(engine,'before_cursor_execute',observe)
    try:cache.refresh(background=False)
    finally:event.remove(engine,'before_cursor_execute',observe)
    assert all(s.lstrip().startswith('SELECT') for s in statements)
    assert all(m['ready'] and not m['error'] for m in cache.status()['modules']),cache.status()
    def offline(*args):raise AssertionError('page accessed source database')
    event.listen(engine,'before_cursor_execute',offline)
    try:
        with TestClient(create_app(config,engine)) as client:
            for path in ['industry-momentum/latest','market-crowding/history','microcap-temperature/history','microcap-breadth/history','dividend-selection/latest','high-growth/latest','earnings-gap/stocks?period=2026-09-30']:
                result=client.get('/api/v1/'+path)
                assert result.status_code==200,result.text
                assert result.headers['x-data-source']=='local-cache'
    finally:event.remove(engine,'before_cursor_execute',offline)

def test_failed_refresh_preserves_snapshot(engine,session,tmp_path):
    model=seed_raw(session,'行业动量');cache=SnapshotCache(settings(tmp_path),engine);cache.refresh('industry-momentum',background=False)
    before=cache.read_state()['modules']['industry-momentum']['file'];row=session.query(model).first();row.payload=b'broken';row.updated_at=STAMP+dt.timedelta(seconds=1);session.commit()
    cache.refresh('industry-momentum',background=False)
    assert cache.read_state()['modules']['industry-momentum']['file']==before and cache.status()['modules'][0]['error']
    with cache.session('industry-momentum') as local:assert local.scalar(select(IndustryMomentum.momentum))==Decimal('.50')

def test_direct_mode_rejects_stale_legacy_data(client,session):
    seed_raw(session,'行业动量');r=client.get('/api/v1/industry-momentum/latest')
    assert r.status_code==503 and 'CACHE_ENABLED' in r.json()['detail']

def test_earnings_first_hit_frozen(engine,session,tmp_path):
    p=payload('净利润断层');seed_raw(session,'净利润断层',p)
    p=copy.deepcopy(p);p['day']=DAY+dt.timedelta(days=3);p['inputs']['caps']['A']['pe_ratio']=99;seed_raw(session,'净利润断层',p)
    cache=SnapshotCache(settings(tmp_path),engine);cache.refresh('earnings-gap',background=False)
    with cache.session('earnings-gap') as local:
        row=local.scalars(select(EarningsStock)).one();assert row.pe_ttm==7 and row.valuation_date==DAY

@pytest.mark.parametrize('name',list(RAW_TABLES))
def test_batch_uses_raw_entry_no_legacy_calculation(monkeypatch,tmp_path,name):
    monkeypatch.setitem(sys.modules,'jqdata',ModuleType('jqdata'))
    spec=importlib.util.spec_from_file_location('raw_batch',ROOT/'collectors/策略批量采集_数据库版.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);mod.PROGRESS_ENABLED=False
    monkeypatch.setattr(mod,'china_now',lambda:STAMP)
    from sqlalchemy import create_engine,inspect
    database=create_engine('sqlite:///'+str(tmp_path/'raw.sqlite'));monkeypatch.setattr(mod,'connect_database',lambda config:database)
    original=mod.STRATEGIES[name]
    def factory(*args):
        ns=original(*args)
        ns.run=lambda **kwargs:pytest.fail('legacy calculation/write path called')
        return ns
    monkeypatch.setitem(mod.STRATEGIES,name,factory)
    config=mod.STRATEGY_CONFIG[name]
    if 'start_date' in config:config['start_date']=str(DAY)
    if 'micro_num' in config:config['micro_num']=2
    monkeypatch.setattr(mod,'_collect_raw',lambda *a:payload(name)['inputs'])
    def calendar(end_date,start_date=None,count=None):
        return list(pd.bdate_range(start=start_date,end=end_date,periods=count).date)
    run=mod._load_strategy(name,mod.DB_CONFIG,{'get_trade_days':calendar})
    assert len(run(skip_existing=True))==1 and len(run(skip_existing=True))==0
    assert inspect(database).get_table_names()==[RAW_TABLES[name][1]]
    with Session(database) as session:assert session.execute(select(RAW_MODELS[RAW_TABLES[name][0]])).all()
    database.dispose()

def test_notebook_python36_self_contained():
    for path in (ROOT/'collectors').glob('*数据库版.py'):
        tree=ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,6))
        assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ('eval','exec','compile','open') for n in ast.walk(tree))
        assert '__file__' not in path.read_text(encoding='utf-8')

def test_same_second_digest_change_and_unchanged_skip(engine,session,tmp_path,monkeypatch):
    model=seed_raw(session,'行业动量');cache=SnapshotCache(settings(tmp_path),engine);cache.refresh('industry-momentum',background=False)
    p=payload('行业动量');frame=p['inputs']['prices'];frame.loc[(frame['code']=='B') & (frame['time']==DAY),'close']=1.3
    session.query(model).delete();session.commit();seed_raw(session,'行业动量',p)  # Same timestamp, different digest.
    cache.refresh('industry-momentum',background=False)
    with cache.session('industry-momentum') as local:assert local.scalar(select(IndustryMomentum.momentum))==2
    import app.core.cache as cache_module
    monkeypatch.setattr(cache_module,'rebuild_raw',lambda *args:pytest.fail('unchanged input should not recompute'))
    cache.refresh('industry-momentum',background=False)
    assert cache.status()['modules'][0]['error'] is None


def test_partial_backfill_and_cleared_source_keep_previous(engine,session,tmp_path):
    model=seed_raw(session,'行业动量');cache=SnapshotCache(settings(tmp_path),engine);cache.refresh('industry-momentum',background=False)
    before=cache.read_state()['modules']['industry-momentum']['file']
    p=payload('行业动量');p['expected_latest_day']=DAY+dt.timedelta(days=3)
    session.query(model).delete();session.commit();seed_raw(session,'行业动量',p)
    cache.refresh('industry-momentum',background=False)
    assert cache.read_state()['modules']['industry-momentum']['file']==before
    session.query(model).delete();session.commit();cache.refresh('industry-momentum',background=False)
    assert cache.read_state()['modules']['industry-momentum']['file']==before
