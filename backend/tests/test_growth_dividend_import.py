from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from app.services import growth_dividend_import as imp
from app.services.growth_dividend import GrowthDividend, CollectionCancelled
from app.core.config import Settings

@pytest.fixture
def source():
    e = create_engine('sqlite://')
    imp.metadata.create_all(e)
    with e.begin() as c:
        c.execute(imp.CALENDAR.insert(), [dict(trade_date=date(2010,1,4), source=imp.CALENDAR_SOURCE, updated_at=datetime(2020,1,1)), dict(trade_date=date(2020,1,2), source=imp.CALENDAR_SOURCE, updated_at=datetime(2020,1,1))])
        c.execute(imp.PRICES.insert(), [dict(index_code='399006.XSHE', trade_date=date(2010,1,4), close=0.000000000123, source=imp.SOURCE, updated_at=datetime(2020,1,1)), dict(index_code='000922.XSHG', trade_date=date(2020,1,2), close=4000.123456789, source=imp.SOURCE, updated_at=datetime(2020,1,1))])
    yield e
    e.dispose()

@pytest.fixture
def monitor(tmp_path):
    return GrowthDividend(Settings(_env_file=None, log_dir=tmp_path, growth_dividend_worker_enabled=False))

def test_snapshot_readonly_precision_archive_and_conflicts(source, monitor):
    sql=[]
    event.listen(source, 'before_cursor_execute', lambda c,cur,s,*args: sql.append(s))
    days, rows = imp.read_source(source,date(2026,1,1),lambda: None)
    assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE')) for s in sql)
    assert rows[0][2] > 0
    assert imp.publish(monitor,days,rows,lambda: None)['inserted']==2
    changed=[(*r[:2], r[2]*2, *r[3:]) for r in rows]
    assert imp.publish(monitor,days,changed,lambda: None)['conflicts']==2
    with monitor.db() as c:
        assert c.execute("SELECT close FROM points WHERE trade_date='2010-01-04'").fetchone()[0]==0.000000000123
    assert all(p['date'] >= '2018-01-01' for p in monitor.history()['points'])

@pytest.mark.parametrize('field,value', [('source','bad'),('index_code','000015.XSHG'),('close',-1),('close',float('inf'))])
def test_bad_source_rejected(source, field, value):
    with source.begin() as c:
        c.execute(imp.PRICES.update().values(**{field:value}))
    with pytest.raises(ValueError): imp.read_source(source,date(2026,1,1),lambda: None)

def test_calendar_crosscheck(source):
    with source.begin() as c: c.execute(imp.CALENDAR.delete())
    with pytest.raises(ValueError): imp.read_source(source,date(2026,1,1),lambda: None)

def test_atomic_cancel(source, monitor):
    days,rows=imp.read_source(source,date(2026,1,1),lambda: None)
    calls=[]
    def cancel():
        calls.append(1)
        if len(calls)==2: raise CollectionCancelled()
    with pytest.raises(CollectionCancelled): imp.publish(monitor,days,rows,cancel)
    with monitor.db() as c: assert c.execute('SELECT count(*) FROM points').fetchone()[0]==0

def test_sync_failure_safe_and_lock_released(monitor):
    def fail(): raise RuntimeError('mysql://password-secret')
    assert imp.start_import(monitor,background=False,engine_factory=fail)
    assert 'secret' not in str(monitor.status())
    assert not monitor.running()

def test_strict_schema(source, monkeypatch):
    real=imp.inspect(source)
    cols=real.get_columns(imp.PRICES.name)
    cols[2]['nullable']=True
    fake=SimpleNamespace(get_columns=lambda n: cols if n==imp.PRICES.name else real.get_columns(n), get_pk_constraint=real.get_pk_constraint)
    monkeypatch.setattr(imp,'inspect',lambda c:fake)
    with source.connect() as c:
        with pytest.raises(ValueError): imp.check_schema(c)

def test_collector_preview_full_calendar_no_config(monkeypatch):
    import pandas as pd
    path=Path(__file__).resolve().parents[2]/'collectors'/'创红比历史补采_聚宽研究版.py'
    spec=importlib.util.spec_from_file_location('jq_growth',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    calls=[]
    def prices(code, **kwargs):
        calls.append(code)
        return pd.DataFrame({'close':[1234.123456789]},index=pd.to_datetime(['2010-01-04']))
    monkeypatch.setitem(sys.modules,'jqdata',SimpleNamespace(get_trade_days=lambda **kw:[date(2010,1,4)],get_price=prices))
    monkeypatch.setitem(sys.modules,'mysql_connector',None)
    result=mod.run(end_date='2010-01-04')
    assert result['start']=='2010-01-04' and len(calls)==3
    assert '000922.XSHG' in calls and '000015.XSHG' not in calls
    assert result['indices']['000688.XSHG']['observed_first_day']=='2010-01-04'

def test_separate_sync_api_removed(client):
    assert client.post('/api/v1/growth-dividend/sync-joinquant').status_code == 404

def test_archive_survives_public_calendar_refresh(source, monitor, monkeypatch):
    from app.services import growth_dividend as service
    from contextlib import nullcontext
    days, rows=imp.read_source(source,date(2026,1,1),lambda: None)
    imp.publish(monitor,days,rows,lambda: None)
    monitor.client_factory=lambda: nullcontext(object())
    monkeypatch.setattr(service,'fetch_calendar',lambda c:[date(2017,12,29),date(2026,12,31)])
    monitor.refresh(background=False)
    with monitor.db() as c:
        assert c.execute("SELECT 1 FROM calendar WHERE trade_date='2010-01-04'").fetchone()
        assert c.execute("SELECT 1 FROM points WHERE trade_date='2010-01-04'").fetchone()

def test_schema_contract_and_python36_syntax():
    import ast
    path=Path(__file__).resolve().parents[2]/'collectors'/'创红比历史补采_聚宽研究版.py'
    ast.parse(path.read_text(),feature_version=(3,6))
    spec=importlib.util.spec_from_file_location('jq_growth_contract',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    for a,b in [(imp.PRICES,mod.PRICES),(imp.CALENDAR,mod.CALENDAR)]:
        assert [(c.name,str(c.type),c.nullable,c.primary_key) for c in a.columns]==[(c.name,str(c.type),c.nullable,c.primary_key) for c in b.columns]
    from decimal import Decimal
    with pytest.raises(ValueError): mod.positive(Decimal('1.123456789012345678901'))


def load_collector():
    path=Path(__file__).resolve().parents[2]/'collectors'/'创红比历史补采_聚宽研究版.py'
    spec=importlib.util.spec_from_file_location('jq_growth_checked',path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('target', ['collector', 'importer'])
def test_unsigned_price_schema_rejected(source, monkeypatch, target):
    from sqlalchemy.dialects.mysql import DOUBLE
    module = load_collector() if target == 'collector' else imp
    real = imp.inspect(source)
    class Inspector:
        def get_columns(self, name):
            columns = [dict(column) for column in real.get_columns(name)]
            for column in columns:
                if column['name'] == 'close':
                    column['type'] = DOUBLE(unsigned=True)
            return columns
        def get_pk_constraint(self, name): return real.get_pk_constraint(name)
        def get_table_options(self, name): return {'mysql_engine': 'InnoDB'}
    monkeypatch.setattr(module, 'inspect', lambda conn: Inspector())
    with pytest.raises(ValueError, match='无符号'):
        module.check_schema(SimpleNamespace(dialect=SimpleNamespace(name='mysql')))


@pytest.mark.parametrize('hour,minute,expected', [(17,59,date(2026,11,9)), (18,0,date(2026,11,10))])
def test_collector_default_cutoff_matches_importer_close_confirmation(monkeypatch, hour, minute, expected):
    import pandas as pd
    from app.services.growth_dividend import CHINA, close_cutoff
    mod = load_collector()
    moment = datetime(2026,11,10,hour,minute)
    monkeypatch.setattr(mod, 'china_now', lambda: moment)
    days = [date(2026,11,9),date(2026,11,10)]
    def calendar(**kwargs):
        assert kwargs['end_date'] == expected
        return [day for day in days if day <= expected]
    def prices(code, **kwargs):
        assert kwargs['end_date'] <= expected
        return pd.DataFrame({'close':[100]}, index=[expected])
    monkeypatch.setitem(sys.modules, 'jqdata', SimpleNamespace(get_trade_days=calendar,get_price=prices))
    result = mod.run()
    assert result['end'] == str(expected)
    assert close_cutoff(moment.replace(tzinfo=CHINA)) == expected


@pytest.mark.parametrize('end', ['2026-11-10', '2026-11-11'])
def test_collector_rejects_unclosed_or_future_dates_before_query(monkeypatch, end):
    mod = load_collector()
    monkeypatch.setattr(mod, 'china_now', lambda: datetime(2026,11,10,17,59))
    def forbidden(**kwargs): pytest.fail('Unconfirmed date must not reach the source')
    monkeypatch.setitem(sys.modules, 'jqdata', SimpleNamespace(get_trade_days=forbidden,get_price=forbidden))
    with pytest.raises(ValueError, match='18:00'):
        mod.run(end_date=end)
