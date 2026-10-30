import ast
import datetime as dt
import math
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, event as sql_event
from sqlalchemy.orm import sessionmaker
from app.models.earnings import EarningsRun as WebRun, EarningsStock as WebStock


class Frame:
    def __init__(self, rows, columns=None):
        self.rows = rows
        self.columns = columns or (list(rows[0]) if rows else [])
        self.empty = not rows
    def __len__(self): return len(self.rows)
    def __getitem__(self, key): return [row[key] for row in self.rows]
    def to_dict(self, orient): return self.rows
    def iterrows(self):
        for row in self.rows:
            yield row['date'], SimpleNamespace(to_dict=lambda row=row: row)


@pytest.fixture
def c(tmp_path):
    source = Path(__file__).resolve().parents[2] / 'collectors' / '净利润断层_数据库版.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and n.module == 'jqdata') and not (
        isinstance(n, ast.Import) and n.names[0].name == 'pandas')]
    env = {'__name__': 'offline_earnings', 'pd': SimpleNamespace(
        isna=lambda v: v is None or isinstance(v, float) and math.isnan(v),
        DataFrame=lambda rows: rows)}
    exec(compile(tree, str(source), 'exec'), env)
    engine = create_engine('sqlite:///' + str(tmp_path / 'earnings.sqlite'))
    env['Base'].metadata.create_all(engine)
    yield SimpleNamespace(env=env, engine=engine, Session=sessionmaker(bind=engine))
    engine.dispose()


def record(c, period=dt.date(2026, 9, 30), code='600001.XSHG'):
    e = c.env['event'](code, 'formal', '2026-10-15', period, 1e8, 25, {})
    result = dict(report_period=period, code=code, name='测试公司', industry_code='test',
        industry_name='测试行业', industry_level='sw_l3', signal_date=dt.date(2026, 10, 15),
        announcement_date=dt.date(2026, 10, 15), signal_source='formal', profit_basis='单季度',
        profit_yoy=25, net_profit=1e8, open_gap_pct=4, intraday_pct=0,
        market_cap=50, circulating_market_cap=40, pe_ttm=-10, pb=0,
        valuation_date=dt.date(2026, 10, 16), events=[e], prices=[])
    return result


def test_schema_parity(c):
    for local, web in [(c.env['EarningsStock'], WebStock), (c.env['EarningsRun'], WebRun)]:
        assert [(x.name, str(x.type), x.primary_key, x.nullable) for x in local.__table__.columns] == [
            (x.name, str(x.type), x.primary_key, x.nullable) for x in web.__table__.columns]


@pytest.mark.parametrize('day,expected', [('2026-10-17','2026-09-30'),('2026-01-01','2025-12-31'),
    ('2026-04-01','2026-03-31'),('2026-09-30','2026-06-30')])
def test_quarter_rollover(c, day, expected):
    assert str(c.env['latest_period'](dt.date.fromisoformat(day))) == expected


@pytest.mark.parametrize('previous,opening,closing,hit', [
    (10,10.3,10.4,False), (10,10.31,10.31,True), (10,10.4,10.39,False),
    (0,10,11,False), (None,10,11,False), (10,float('nan'),11,False)])
def test_price_boundary(c,previous,opening,closing,hit):
    assert (c.env['price_signal'](previous,opening,closing) is not None) == hit


@pytest.mark.parametrize('kind,profit,growth,eligible', [
    ('express',1,20,True),('formal',0,25,False),('formal',-1,30,False),
    ('forecast',None,20,True),('forecast',None,19.99,False),('formal',10,None,False)])
def test_financial_eligibility(c,kind,profit,growth,eligible):
    e=c.env['event']('600001.XSHG',kind,'2026-10-15',dt.date(2026,9,30),profit,growth,{})
    assert e['eligible'] is eligible




def test_atomic_replace_idempotence_zero_and_other_quarters(c):
    save=c.env['save_results']; row=record(c); p=row['report_period']; day=row['valuation_date']
    old=record(c,dt.date(2026,6,30))
    save(c.Session,old['report_period'],day,6000,[old])
    save(c.Session,p,day,6000,[row])
    save(c.Session,p,day,6000,[row])
    with c.Session() as s:
        assert s.query(c.env['EarningsStock']).count()==2
        assert all(row.analysis == '' and row.analysis_basis == '' for row in s.query(c.env['EarningsStock']).all())
    save(c.Session,p,day,6000,[])
    with c.Session() as s:
        assert s.query(c.env['EarningsStock']).count()==1
        assert s.get(c.env['EarningsRun'],p).matched_count==0


def test_failure_rolls_back_previous_snapshot(c):
    row=record(c); save=c.env['save_results']
    save(c.Session,row['report_period'],row['valuation_date'],6000,[row])
    def fail(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith('INSERT INTO earnings_gap_stock'):
            raise RuntimeError('synthetic write failure')
    sql_event.listen(c.engine,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError):
            save(c.Session,row['report_period'],row['valuation_date'],6000,[dict(row,name='changed')])
    finally:
        sql_event.remove(c.engine,'before_cursor_execute',fail)
    with c.Session() as s:
        assert s.query(c.env['EarningsStock']).one().name=='测试公司'


def test_duplicate_and_mixed_period_rejected(c):
    row=record(c)
    with pytest.raises(ValueError):
        c.env['save_results'](c.Session,row['report_period'],row['valuation_date'],6000,[row,row])
    with pytest.raises(ValueError):
        c.env['save_results'](c.Session,dt.date(2026,6,30),row['valuation_date'],6000,[row])


def test_keyset_pagination_does_not_stop_on_short_pages(c):
    from sqlalchemy import Column,Integer
    class Table(c.env['Base']):
        __tablename__='source'
        id=Column(Integer,primary_key=True)
    c.env['Base'].metadata.create_all(c.engine)
    with c.Session() as s:
        s.add_all([Table(id=i) for i in range(1,6)]);s.commit()
        c.env['query']=s.query
        calls=[]
        def fetch(q):
            ids=[r.id for r in q.limit(2).all()]
            calls.append(ids)
            return Frame([{'id':i} for i in ids],['id'])
        stream = c.env['paged'](Table,[],fetch)
        assert not calls  # Pagination is lazy until consumed.
        assert len(list(stream))==5
        assert calls==[[1,2],[3,4],[5],[]]


def test_signal_weekend_matches_next_day_not_previous_and_paused_excluded(c):
    period=dt.date(2026,9,30); end=dt.date(2026,10,19)
    e=c.env['event']('600001.XSHG','express','2026-10-17',period,1e8,30,{})
    days=[dt.date(2026,10,d) for d in [15,16,19]]
    c.env['get_trade_days']=lambda **kw:days
    bars=[dict(date=days[0],open=10,close=10,high=10,low=10,paused=0,volume=100),
          dict(date=days[1],open=10,close=10,high=10,low=10,paused=0,volume=100),
          dict(date=days[2],open=10.4,close=10.5,high=10.6,low=9,paused=0,volume=100)]
    c.env['get_price']=lambda *a,**k:Frame(bars)
    c.env['get_industry']=lambda codes,**k:{codes[0]:{'sw_l3':{'industry_code':'test','industry_name':'测试行业'}}}
    # Real SQL expressions are not required for the valuation stub.
    field=SimpleNamespace(in_=lambda codes:True)
    c.env['valuation']=SimpleNamespace(code=field,market_cap=1,circulating_market_cap=1,pe_ratio=1,pb_ratio=1)
    c.env['query']=lambda *a:SimpleNamespace(filter=lambda *a:None)
    c.env['get_fundamentals']=lambda *a,**k:Frame([dict(code='600001.XSHG',market_cap=50,circulating_market_cap=30,pe_ratio=10,pb_ratio=2)])
    result=c.env['screen'](['600001.XSHG'],{'600001.XSHG':'测试公司'},[e],period,end)
    assert len(result)==1 and result[0]['signal_date']==days[2]
    assert result[0]['open_gap_pct']==4  # The low price need not form a strict gap.
    assert 'analysis' not in result[0] and result[0]['events'][0]['signal']
    bars[-1]['paused']=1
    assert c.env['screen'](['600001.XSHG'],{'600001.XSHG':'测试公司'},[e],period,end)==[]


def test_source_failure_never_connects_or_overwrites(c):
    c.env['china_now']=lambda:dt.datetime(2026,10,17,18)
    c.env['get_trade_days']=lambda **k:[dt.date(2026,10,16)]
    class Universe(Frame):
        def iterrows(self): yield '600001.XSHG',{'display_name':'测试公司'}
    c.env['get_all_securities']=lambda **k:Universe([{'display_name':'测试公司'}])
    def fail(*a): raise RuntimeError('source unavailable')
    c.env['fetch_events']=fail
    c.env['MysqlEarnings'].connect_to_mysql=lambda:pytest.fail('must not connect')
    with pytest.raises(RuntimeError): c.env['run'](skip_existing=False)



def test_real_query_filters_three_sources_and_preserves_source_records(c):
    from sqlalchemy import Column, Date, Integer, Float, String
    class Securities(c.env['Base']):
        __tablename__='fake_securities'
        ID=Column(Integer,primary_key=True)
        CompanyCode=Column(Integer)
        SecuCode=Column(String)
        SecuCategory=Column(Integer)
    class Express(c.env['Base']):
        __tablename__='fake_express'
        ID=Column(Integer,primary_key=True)
        CompanyCode=Column(Integer)
        EndDate=Column(Date)
        InfoPublDate=Column(Date)
        NPParentCompanyOwners=Column(Float)
        NPCOwnersYOY=Column(Float)
    class Forecast(c.env['Base']):
        __tablename__='fake_forecast'
        id=Column(Integer,primary_key=True)
        code=Column(String)
        end_date=Column(Date)
        pub_date=Column(Date)
        profit_ratio_min=Column(Float)
        profit_ratio_max=Column(Float)
    class Income(c.env['Base']):
        __tablename__='fake_income'
        code=Column(String,primary_key=True)
        pubDate=Column(Date)
        statDate=Column(Date)
        np_parent_company_owners=Column(Float)
    class Indicator(c.env['Base']):
        __tablename__='fake_indicator'
        code=Column(String,primary_key=True)
        pubDate=Column(Date)
        statDate=Column(Date)
        inc_net_profit_to_shareholders_year_on_year=Column(Float)
    c.env['Base'].metadata.create_all(c.engine)
    period=dt.date(2026,9,30); as_of=dt.date(2026,10,16)
    with c.Session() as s:
        s.add(Securities(ID=1,CompanyCode=1,SecuCode='600001',SecuCategory=1))
        s.add_all([
            Express(ID=1,CompanyCode=1,EndDate=period,InfoPublDate=dt.date(2026,10,15),NPParentCompanyOwners=1e8,NPCOwnersYOY=25),
            Express(ID=2,CompanyCode=1,EndDate=period,InfoPublDate=dt.date(2026,10,18),NPParentCompanyOwners=1e8,NPCOwnersYOY=999),
            Express(ID=3,CompanyCode=1,EndDate=dt.date(2026,6,30),InfoPublDate=dt.date(2026,8,15),NPParentCompanyOwners=1e8,NPCOwnersYOY=999),
            Forecast(id=1,code='600001.XSHG',end_date=period,pub_date=dt.date(2026,10,14),profit_ratio_min=30,profit_ratio_max=45),
        ])
        s.commit()
        c.env['query']=s.query
        def fetch(q):
            model=q.column_descriptions[0]['entity']
            columns=[col.key for col in model.__table__.columns]
            return Frame([{key:getattr(row,key) for key in columns} for row in q.all()],columns)
        c.env['jy']=SimpleNamespace(SecuMain=Securities,LC_FSPerformedLetters=Express,run_query=fetch)
        c.env['finance']=SimpleNamespace(STK_FIN_FORCAST=Forecast,run_query=fetch)
        c.env['income']=Income;c.env['indicator']=Indicator
        calls=[]
        def formal(q,date):
            calls.append(date)
            return Frame([dict(code='600001.XSHG',publication_date=dt.date(2026,10,15),
                report_period=period,net_profit=1e8,profit_yoy=35,indicator_publication_date=dt.date(2026,10,16),
                indicator_period=period)])
        c.env['get_fundamentals']=formal
        events=c.env['fetch_events'](['600001.XSHG'],period,as_of)
        assert len(events)==3 and calls==[as_of]
        by_kind={e['kind']:e for e in events}
        assert by_kind['formal']['publication_date']=='2026-10-16'
        assert by_kind['forecast']['profit_yoy_max']==45
        assert by_kind['express']['profit_yoy']==25
        assert by_kind['express']['raw']['EndDate']=='2026-09-30'
        assert all(e['report_period']=='2026-09-30' for e in events)


@pytest.mark.parametrize('field,value,expected', [
    ('open_gap_pct', 3.123456789012345, '3.123457'),
    ('intraday_pct', 0.3333333333333333, '0.333333'),
    ('pe_ttm', '-1.2345675', '-1.234568'),
    ('pb', 0, '0.000000'),
    ('market_cap', '123.1234565', '123.123457'),
    ('circulating_market_cap', '80.1234564', '80.123456'),
    ('profit_yoy', '25.1234565', '25.123457'),
    ('net_profit', '1234567890123456789.12345', '1234567890123456789.1235'),
    ('net_profit', None, None),
])
def test_database_decimal_quantization(c, field, value, expected):
    from decimal import Decimal
    row = record(c)
    row[field] = value
    normalized = c.env['database_record'](row)
    assert normalized[field] == (None if expected is None else Decimal(expected))
    if expected is not None:
        assert isinstance(normalized[field], Decimal)
        assert str(normalized[field]) == expected
    assert row[field] == value  # Do not alter computed/source data.
    assert normalized['events'] is row['events']


@pytest.mark.parametrize('field,value', [
    ('open_gap_pct', None), ('pb', float('nan')), ('pe_ttm', float('inf')),
    ('market_cap', '-Infinity'), ('net_profit', 'bad'),
    ('open_gap_pct', '100000000000000'),
    ('open_gap_pct', '99999999999999.9999995'),  # Overflow only after rounding.
    ('net_profit', '1000000000000000000000000'),
])
def test_invalid_decimals_rejected_before_opening_write_transaction(c, field, value):
    row = record(c)
    row[field] = value
    with pytest.raises(ValueError):
        c.env['database_record'](row)


def test_mysql_bind_preserves_decimal_without_float_tail(c):
    from decimal import Decimal
    from sqlalchemy.dialects.mysql.mysqldb import dialect
    from pymysql.converters import escape_item, conversions
    row = record(c)
    row.update(open_gap_pct=3.123456789012345, intraday_pct=0.3333333333333333)
    normalized = c.env['database_record'](row)
    mysql = dialect()
    for column in c.env['EarningsStock'].__table__.columns:
        if column.name not in ('open_gap_pct', 'intraday_pct'):
            continue
        processor = column.type.dialect_impl(mysql).bind_processor(mysql)
        value = normalized[column.name]
        bound = processor(value) if processor else value
        assert isinstance(bound, Decimal)
        literal = escape_item(bound, 'utf8mb4', conversions)
        assert literal in ('3.123457', '0.333333')


def test_save_quantizes_after_raw_threshold_and_preserves_old_rows_on_invalid_value(c):
    from decimal import Decimal
    from sqlalchemy.orm import Session
    row = record(c)
    row.update(open_gap_pct=3.00000001, intraday_pct=1 / 3)
    seen = []
    def capture(session, flush_context, instances):
        for item in session.new:
            if isinstance(item, c.env['EarningsStock']):
                seen.append((item.open_gap_pct, item.intraday_pct))
    sql_event.listen(Session, 'before_flush', capture)
    try:
        c.env['save_results'](c.Session, row['report_period'], row['valuation_date'], 6000, [row])
    finally:
        sql_event.remove(Session, 'before_flush', capture)
    assert seen == [(Decimal('3.000000'), Decimal('0.333333'))]
    assert all(isinstance(v, Decimal) for v in seen[0])
    assert row['open_gap_pct'] == 3.00000001  # Eligible before storage rounding.
    bad = dict(row, pe_ttm=float('inf'))
    with pytest.raises(ValueError):
        c.env['save_results'](lambda: pytest.fail('must fail before opening session'),
                              row['report_period'], row['valuation_date'], 6000, [bad])
    with c.Session() as s:
        assert s.query(c.env['EarningsStock']).count() == 1
        assert s.query(c.env['EarningsStock']).one().name == row['name']


def test_insert_only_earnings_retains_first_snapshot_and_accumulates_new_stocks(c):
    save=c.env['save_results']; a=record(c); p=a['report_period']; day=a['valuation_date']
    older=record(c,dt.date(2026,6,30))
    save(c.Session,older['report_period'],day,6000,[older])
    save(c.Session,p,day,6000,[a],skip_existing=True)
    b=record(c,code='600002.XSHG')
    fresh=save(c.Session,p,day,6000,[dict(a,name='should not replace',pe_ttm=99),b],skip_existing=True)
    assert [r['code'] for r in fresh]==[b['code']]
    statements=[]
    def capture(conn,cursor,statement,*args):
        if statement.lstrip().split()[0].upper() in ('INSERT','UPDATE','DELETE'):
            statements.append(statement)
    sql_event.listen(c.engine,'before_cursor_execute',capture)
    try:
        assert save(c.Session,p,day,6000,[b],skip_existing=True)==[]
        assert save(c.Session,p,day,6000,[],skip_existing=True)==[]
    finally:
        sql_event.remove(c.engine,'before_cursor_execute',capture)
    assert not statements
    with c.Session() as s:
        row=s.get(c.env['EarningsStock'],(p,a['code']))
        assert row.name==a['name'] and row.pe_ttm==a['pe_ttm']
        assert s.query(c.env['EarningsStock']).count()==3
        assert s.get(c.env['EarningsRun'],p).matched_count==2


def test_insert_only_earnings_rolls_back_new_rows_and_metadata(c):
    row=record(c); p=row['report_period']; day=row['valuation_date']; save=c.env['save_results']
    save(c.Session,p,day,6000,[row],skip_existing=True)
    class FailCommit(c.Session.class_):
        def commit(self):
            self.flush()
            raise RuntimeError('synthetic commit failure')
    with pytest.raises(RuntimeError):
        save(sessionmaker(bind=c.engine,class_=FailCommit),p,day,6000,[record(c,code='600002.XSHG')],skip_existing=True)
    with c.Session() as s:
        assert s.query(c.env['EarningsStock']).count()==1
        assert s.get(c.env['EarningsRun'],p).matched_count==1


def test_insert_only_earnings_skips_fetching_already_saved_stocks(c):
    a=record(c); b=record(c,code='600002.XSHG'); day=a['valuation_date']
    c.env['save_results'](c.Session,a['report_period'],day,2,[a],skip_existing=True)
    c.env['china_now']=lambda:dt.datetime(2026,10,17,17)
    c.env['get_trade_days']=lambda **kwargs:[day]
    c.env['get_all_securities']=lambda **kwargs:SimpleNamespace(
        empty=False,columns=['display_name'],iterrows=lambda:iter([(a['code'],{'display_name':'A'}),(b['code'],{'display_name':'B'})]))
    c.env['MysqlEarnings']=SimpleNamespace(connect_to_mysql=lambda:c.engine)
    seen=[]
    def fetch(codes,*args):
        seen.append(list(codes))
        return []
    c.env['fetch_events']=fetch
    c.env['screen']=lambda codes,*args:[b]
    result=c.env['run'](skip_existing=True)
    assert seen==[[b['code']]] and [r['code'] for r in result]==[b['code']]
    c.env['fetch_events']=lambda *args:pytest.fail('already saved')
    c.env['screen']=lambda *args:pytest.fail('already saved')
    assert c.env['run'](skip_existing=True)==[]


def test_quarter_revision_append_noop_and_rebuild(c):
    from sqlalchemy import select
    table = c.env['_earnings_version_table']()
    save, item = c.env['save_results'], record(c)
    period, day = item['report_period'], item['valuation_date']
    def version():
        with c.engine.connect() as connection:
            return dict(connection.execute(select(table)).mappings().one())
    save(c.Session, period, day, 6000, [item], skip_existing=True)
    first = version()
    assert first['revision'] == 1
    save(c.Session, period, day, 6000, [item], skip_existing=True)
    assert version() == first
    save(c.Session, period, day, 6000, [record(c, code='000001.XSHE')], skip_existing=True)
    second = version()
    assert second['generation'] == first['generation'] and second['revision'] == 2
    save(c.Session, period, day, 6000, [item], skip_existing=False)
    rebuilt = version()
    assert rebuilt['generation'] != first['generation'] and rebuilt['revision'] == 3
    def fail(conn, cursor, statement, *args):
        if statement.startswith('INSERT INTO earnings_gap_stock'):
            raise RuntimeError('simulated write failure')
    sql_event.listen(c.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError):
            save(c.Session, period, day, 6000, [item], skip_existing=False)
    finally:
        sql_event.remove(c.engine, 'before_cursor_execute', fail)
    assert version() == rebuilt
