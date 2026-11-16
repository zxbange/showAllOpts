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
    assert imp.publish(monitor,days,changed,lambda: None)['updated']==2
    with monitor.db() as c:
        assert c.execute("SELECT close FROM points WHERE trade_date='2010-01-04'").fetchone()[0]==0.000000000246
    assert all(p['date'] >= '2018-01-01' for p in monitor.history()['points'])

@pytest.mark.parametrize('field,value', [('source','bad'),('index_code','000015.XSHG'),('close',-1),('close',float('inf'))])
def test_bad_source_rejected(source, field, value):
    with source.begin() as c:
        c.execute(imp.PRICES.update().values(**{field:value}))
    with pytest.raises((ValueError, imp.SnapshotDataError)): imp.read_source(source,date(2026,1,1),lambda: None)

def test_calendar_crosscheck(source):
    with source.begin() as c: c.execute(imp.CALENDAR.delete())
    with pytest.raises((ValueError, imp.SnapshotDataError)): imp.read_source(source,date(2026,1,1),lambda: None)

def test_atomic_cancel(source, monitor):
    days,rows=imp.read_source(source,date(2026,1,1),lambda: None)
    calls=[]
    def cancel():
        calls.append(1)
        if len(calls)==2: raise CollectionCancelled()
    with pytest.raises(CollectionCancelled): imp.publish(monitor,days,rows,cancel)
    with monitor.db() as c: assert c.execute('SELECT count(*) FROM points').fetchone()[0]==0


def test_strict_schema(source, monkeypatch):
    real=imp.inspect(source)
    cols=real.get_columns(imp.PRICES.name)
    cols[2]['nullable']=True
    fake=SimpleNamespace(get_columns=lambda n: cols if n==imp.PRICES.name else real.get_columns(n), get_pk_constraint=real.get_pk_constraint)
    monkeypatch.setattr(imp,'inspect',lambda c:fake)
    with source.connect() as c:
        with pytest.raises(ValueError): imp.check_schema(c)


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


def test_unsigned_price_schema_rejected(source, monkeypatch):
    from sqlalchemy.dialects.mysql import DOUBLE
    module = imp
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
