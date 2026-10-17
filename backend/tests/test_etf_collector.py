"""Exercise collector transactions and API response validation without JoinQuant/MySQL."""
import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Date, String, BigInteger, create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.etf_catalog import ETF_GROUPS
from app.models.etf import ETFShare as WebETFShare


class Frame:
    columns = ['code', 'date', 'shares']
    def __init__(self, rows): self.rows=rows
    def to_dict(self, orient): return self.rows


@pytest.fixture
def collector(tmp_path):
    source=Path(__file__).resolve().parents[2]/'collectors'/'ETF份额_数据库版.py'
    tree=ast.parse(source.read_text(encoding='utf-8'))
    tree.body=[n for n in tree.body if not (isinstance(n,ast.ImportFrom) and n.module=='jqdata')
               and not (isinstance(n,ast.Import) and n.names[0].name=='pandas')]
    env={'__name__':'offline_etf_test','pd':SimpleNamespace(DataFrame=lambda data=None,columns=None:data or [])}
    exec(compile(tree,str(source),'exec'),env)
    env['china_now']=lambda:dt.datetime(2026,10,17,10)
    engine=create_engine('sqlite:///'+str(tmp_path/'etf.sqlite'))
    env['Base'].metadata.create_all(engine)
    class Source(env['Base']):
        __tablename__='fake_source'
        code=Column(String,primary_key=True)
        date=Column(Date,primary_key=True)
        shares=Column(BigInteger)
    source_session=sessionmaker(bind=engine)()
    env['query']=source_session.query
    days=[dt.date(2026,10,day) for day in (9,12,13,14,15,16)]
    def calendar(start_date=None,end_date=None,count=None):
        result=[day for day in [dt.date(2025,10,17)]+days if (start_date is None or day>=start_date) and day<=end_date]
        return result[-count:] if count else result
    env['get_trade_days']=calendar
    def api(query):
        compiled=query.statement.compile(compile_kwargs={'literal_binds':True})
        # Real SQLAlchemy expression, but no source database/API is contacted.
        params=query.statement.compile().params
        code=params['code_1']; first=params['date_1']; last=params['date_2']
        requested.append((code,first,last))
        assert 'LIMIT' in str(compiled)
        return Frame([dict(code=code,date=day,shares=value) for (fund,day),value in source_data.items()
                      if fund==code and first<=day<=last])
    env['finance']=SimpleNamespace(FUND_SHARE_DAILY=Source,run_query=api)
    env['MysqlETF']=SimpleNamespace(connect_to_mysql=lambda:engine)
    source_data={}; requested=[]
    yield SimpleNamespace(env=env,engine=engine,Session=sessionmaker(bind=engine),days=days,
                          source=source_data,requested=requested)
    source_session.close();engine.dispose()


def rows(c):
    with c.Session() as s:
        return s.query(c.env['ETFShare']).order_by(c.env['ETFShare'].fund_code,c.env['ETFShare'].trade_date).all()


def record(c,day,value=100,code='510300.XSHG'):
    return dict(fund_code=code,fund_name='test',trade_date=day,shares=value,
                previous_trade_date=c.days[c.days.index(day)-1],source='jqdata.FUND_SHARE_DAILY',
                updated_at=dt.datetime(2026,10,17))


def test_catalog_schema_and_default_year(collector):
    c=collector
    assert c.env['ETF_GROUPS']==ETF_GROUPS
    assert c.env['START_DATE'] is None and c.env['END_DATE'] is None
    columns=c.env['ETFShare'].__table__.columns
    assert [(x.name,str(x.type),x.primary_key,x.nullable) for x in columns]==[
        (x.name,str(x.type),x.primary_key,x.nullable) for x in WebETFShare.__table__.columns]
    # Capture the requested calendar start rather than sparse test calendar's first day.
    starts=[]; calendar=c.env['get_trade_days']
    def capture(**kwargs):
        if 'start_date' in kwargs: starts.append(kwargs['start_date'])
        return calendar(**kwargs)
    c.env['get_trade_days']=capture
    c.env['run'](groups=['hs300'])
    assert starts and set(starts)=={dt.date(2025,10,18)}


def test_incremental_per_fund_refresh_and_idempotence(collector):
    c=collector
    c.env['ETF_GROUPS']={'hs300':ETF_GROUPS['hs300'][:2]}
    for code,last in [('510300.XSHG',c.days[2]),('510310.XSHG',c.days[3])]:
        for day in c.days[1:c.days.index(last)+1]:
            c.env['save_window'](c.Session,code,[record(c,day,100,code)])
        for day in c.days[1:]: c.source[code,day]=200
    result=c.env['run'](groups=['hs300'],lookback_days=1)
    assert [(x[0],x[1]) for x in c.requested]==[('510300.XSHG',c.days[2]),('510310.XSHG',c.days[3])]
    assert len(result)==7 and len(rows(c))==10
    c.env['run'](groups=['hs300'],lookback_days=1)
    assert len(rows(c))==10
    assert all(r.shares==200 for r in rows(c) if r.trade_date>=c.days[3])


def test_first_collection_only_published_records_no_zero_fill(collector):
    c=collector;c.env['ETF_GROUPS']={'hs300':ETF_GROUPS['hs300'][:1]}
    # The calendar includes a prior trade day outside the selected range.
    for day in c.days[2:4]:c.source['510300.XSHG',day]=0
    result=c.env['run'](start_date=c.days[1])
    assert [r['trade_date'] for r in result]==c.days[2:4] and all(r.shares==0 for r in rows(c))


@pytest.mark.parametrize('value',[None,float('nan'),float('inf'),-1,1.5,'bad',2**63])
def test_invalid_shares(collector,value):
    with pytest.raises(ValueError):collector.env['normalize_shares'](value)


def test_source_hole_and_leading_loss_do_not_advance(collector):
    c=collector;c.env['ETF_GROUPS']={'hs300':ETF_GROUPS['hs300'][:1]}
    for day in c.days[1:3]: c.env['save_window'](c.Session,'510300.XSHG',[record(c,day)])
    c.source.update({('510300.XSHG',day):100 for day in c.days[2:]})
    with pytest.raises(RuntimeError):c.env['run'](lookback_days=2)
    assert max(r.trade_date for r in rows(c))==c.days[2]
    c.source['510300.XSHG',c.days[1]]=100
    del c.source['510300.XSHG',c.days[3]]
    with pytest.raises(RuntimeError):c.env['run'](lookback_days=2)
    assert len(rows(c))==2


def test_duplicate_wrong_code_and_nontrading_response(collector):
    c=collector
    for bad in ([dict(code='wrong',date=c.days[1],shares=1)],
                [dict(code='510300.XSHG',date=c.days[1],shares=1)]*2,
                [dict(code='510300.XSHG',date=dt.date(2026,10,11),shares=1)]):
        c.env['finance'].run_query=lambda q:Frame(bad)
        with pytest.raises(ValueError):c.env['fetch_window']('510300.XSHG','name',c.days[1:],dict(zip(c.days[1:],c.days[:-1])))


def test_rollback_and_other_fund_continues(collector):
    c=collector
    c.env['save_window'](c.Session,'510300.XSHG',[record(c,c.days[1])])
    def fail(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith('INSERT INTO etf_share_daily'):raise RuntimeError('insert failed')
    event.listen(c.engine,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError):c.env['save_window'](c.Session,'510300.XSHG',[record(c,c.days[1],200)])
    finally:event.remove(c.engine,'before_cursor_execute',fail)
    assert rows(c)[0].shares==100
    c.env['ETF_GROUPS']={'hs300':ETF_GROUPS['hs300'][:2]}
    for day in c.days[1:]:
        c.source['510300.XSHG',day]=-1
        c.source['510310.XSHG',day]=300
    with pytest.raises(RuntimeError):c.env['run'](start_date=c.days[1])
    assert len([r for r in rows(c) if r.fund_code=='510310.XSHG'])==5


def test_windowing_and_manual_backfill(collector):
    c=collector;c.env['ETF_GROUPS']={'hs300':ETF_GROUPS['hs300'][:1]};c.env['WINDOW_SIZE']=2
    for day in c.days[1:]:c.source['510300.XSHG',day]=100
    c.env['run'](start_date=c.days[1])
    assert len(c.requested)==3 and len(rows(c))==5
    c.requested.clear(); c.source['510300.XSHG',c.days[1]]=200
    c.env['run'](start_date=c.days[1],end_date=c.days[2],incremental=False)
    assert rows(c)[0].shares==200 and len(rows(c))==5


def test_probe_and_invalid_parameters_do_not_connect(collector):
    c=collector
    def forbidden():raise AssertionError('database touched')
    c.env['MysqlETF'].connect_to_mysql=forbidden
    for day in c.days[1:]:c.source['510300.XSHG',day]=123
    assert len(c.env['probe']())==5
    for kwargs in ({'end_date':'2099-01-01'},{'lookback_days':0},{'groups':['bad']},{'incremental':'yes'}):
        with pytest.raises(ValueError):c.env['run'](**kwargs)
    del c.env['finance']
    with pytest.raises(ValueError):c.env['run']()


def test_long_pause_resumes_before_rolling_year_and_partial_window_checkpoint(collector):
    c=collector;c.env['ETF_GROUPS']={'hs300':ETF_GROUPS['hs300'][:1]}
    old=dt.date(2025,1,2)
    c.env['save_window'](c.Session,'510300.XSHG',[dict(record(c,c.days[1]),trade_date=old,previous_trade_date=dt.date(2024,12,31))])
    requested=[]
    def calendar(start_date=None,end_date=None,count=None):
        values=[dt.date(2024,12,31),old]+c.days
        if start_date is not None:requested.append(start_date)
        result=[d for d in values if (start_date is None or d>=start_date) and d<=end_date]
        return result[-count:] if count else result
    c.env['get_trade_days']=calendar
    c.source['510300.XSHG',old]=100
    for day in c.days:c.source['510300.XSHG',day]=100
    c.env['WINDOW_SIZE']=2
    c.source['510300.XSHG',c.days[2]]=-1
    with pytest.raises(RuntimeError):c.env['run'](lookback_days=1)
    assert requested[0]==old  # A year-long outage must not skip uncollected history.
    assert [r.trade_date for r in rows(c)]==[old,c.days[0]]
    c.source['510300.XSHG',c.days[2]]=100
    c.env['run'](lookback_days=1)
    assert len(rows(c))==7 and rows(c)[-1].trade_date==c.days[-1]


def test_notebook_numpy_any_does_not_shadow_python_validation(collector):
    c=collector
    def shadowed(*args):raise AssertionError('Notebook any must not be called')
    c.env['any']=shadowed
    c.env['ETF_GROUPS']={'hs300':ETF_GROUPS['hs300'][:1]}
    for day in c.days[1:]:c.source['510300.XSHG',day]=100
    assert len(c.env['run'](start_date=c.days[1]))==5
