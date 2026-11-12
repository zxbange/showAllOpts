"""Scheduled notifications follow committed business changes only."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
import pytest
from app.services import collection_notifications as n

@pytest.fixture
def settings(tmp_path,monkeypatch):
    monkeypatch.setattr(n,'start_notifications',lambda settings:None)
    monkeypatch.setattr(n,'enabled',lambda settings,task:True)
    return SimpleNamespace(log_path=tmp_path)

def rows(settings):
    db=n._database(settings)
    try:return [dict(row) for row in db.execute('SELECT * FROM outbox')]
    finally:db.close()

def emit(settings,**overrides):
    arguments=dict(scheduled=True,run_key='run-1',outcome='completed',changed_count=1,summary=['数据日期：2026-11-11'])
    arguments.update(overrides)
    return n.emit_collection_result(settings,'cci',**arguments)

def test_manual_no_change_and_disabled_are_silent(settings,monkeypatch):
    assert not emit(settings,scheduled=False)
    assert not emit(settings,changed_count=0,outcome='failed')
    monkeypatch.setattr(n,'enabled',lambda *_:False)
    assert not emit(settings)
    assert rows(settings)==[]

def test_persistent_dedup_partial_update_and_formatted_content(settings):
    assert emit(settings,outcome='partial')
    assert not emit(settings,outcome='partial')
    row,=rows(settings)
    assert row['status']=='pending'
    assert 'CCI 动力煤' in row['message']
    assert '失败（部分数据已更新）' in row['message']
    assert '采集时间：' in row['message'] and '更新记录：1' in row['message']

def fake_social(monkeypatch,settings,*,fail=False):
    from app.services import social
    sent=[]
    class Fake:
        def __init__(self,_):self.root=settings.log_path
        @contextmanager
        def db(self):yield None
        def config(self,_):return {'token':'encrypted-token','chat_id':'encrypted-chat','enabled':False,'telegram_enabled':False}
        def decode(self,value):return value
        def send_part(self,token,chat,text):
            if fail:raise RuntimeError('DO_NOT_LOG_SECRET')
            sent.append(text)
    monkeypatch.setattr(social,'SocialMonitor',Fake)
    return sent

def test_delivery_independent_of_social_switch_and_disable_cancels_queue(settings,monkeypatch):
    sent=fake_social(monkeypatch,settings)
    emit(settings);n.deliver_pending(settings);n.deliver_pending(settings)
    assert len(sent)==1 and rows(settings)[0]['status']=='sent'
    emit(settings,run_key='run-2')
    monkeypatch.setattr(n,'enabled',lambda *_:False)
    n.deliver_pending(settings)
    assert rows(settings)[1]['status']=='skipped' and len(sent)==1

def test_send_failure_is_bounded_and_secrets_not_logged(settings,monkeypatch,caplog):
    fake_social(monkeypatch,settings,fail=True);emit(settings)
    for _ in range(3):
        db=n._database(settings)
        with db:db.execute('UPDATE outbox SET next_try=0')
        db.close();n.deliver_pending(settings)
    row,=rows(settings)
    assert row['attempts']==3 and row['status']=='failed'
    assert 'DO_NOT_LOG_SECRET' not in caplog.text

class Monitor:
    def __init__(self,settings):
        self.settings=settings;self.path=settings.log_path/'points.sqlite'
        with self.db() as db:
            db.execute('CREATE TABLE points(trade_date TEXT PRIMARY KEY,ratio REAL,updated_at TEXT)')
            db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT)')
            db.execute('INSERT INTO state VALUES (1,?)',(json.dumps({}),))
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path)
        try:
            with db:yield db
        finally:db.close()
    @n.notify_collection('margin')
    def collect(self,lock,state,*,value=120,fail=False):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO points VALUES (?,?,?)',('2026-11-11',value,'new timestamp'))
            db.execute('UPDATE state SET payload=?',(json.dumps({'error':'safe failure' if fail else None}),))
        lock.release()

class Lock:
    def __init__(self,callback=lambda:None):self.callback=callback
    def release(self):self.callback()

def test_hooks_snapshot_before_release_and_ignore_timestamp_only(settings):
    m=Monitor(settings)
    m.collect(Lock(),{'run_id':'1','current_scheduled_slot':'2026-11-12'})
    m.collect(Lock(),{'run_id':'2','current_scheduled_slot':'2026-11-12'})
    assert len(rows(settings))==1
    m.collect(Lock(),{'run_id':'3','current_scheduled_slot':None},value=130)
    assert len(rows(settings))==1
    m.collect(Lock(),{'run_id':'4','current_scheduled_slot':'2026-11-12'},value=140,fail=True)
    assert '失败' in rows(settings)[1]['message']

def test_all_templates(settings):
    assert '—' in n.summarize_changes('commodities',{'2026-11-11':1},{'2026-11-11':dict(low=None,high=None,mean=21000)})[1]
    assert '近3年' in n.summarize_changes('cash_cow',{3:[]},{3:[]})[0]
    assert '创红比：0.500000' in n.summarize_changes('growth_dividend',{('2026-11-11','399006'):2500},{('2026-11-11','399006'):2500,('2026-11-11','000922'):5000})

def test_snapshot_is_taken_before_business_lock_release(settings):
    monitor=Monitor(settings)
    def competing_writer():
        with monitor.db() as db:db.execute('UPDATE points SET ratio=999')
    monitor.collect(Lock(competing_writer),{'run_id':'race','current_scheduled_slot':'2026-11-12'},value=120)
    assert '最新担保比例：120.0%' in rows(settings)[0]['message']
    assert '999' not in rows(settings)[0]['message']

def test_notification_failure_cannot_fail_collection(settings,monkeypatch):
    monitor=Monitor(settings)
    def unavailable(*args,**kwargs):raise OSError('outbox unavailable')
    monkeypatch.setattr(n,'_database',unavailable)
    monitor.collect(Lock(),{'run_id':'safe','current_scheduled_slot':'2026-11-12'})
    with monitor.db() as db:assert db.execute('SELECT ratio FROM points').fetchone()[0]==120

@pytest.mark.parametrize('task,table,rows_sql,first,second',[
 ('commodities','quotes(day TEXT,payload TEXT)','INSERT INTO quotes VALUES (?,?)',
 ('2026-11-11',json.dumps(dict(low=1,high=3,mean=2,change=0,rangeEvidence='page',fetchedAt='old'))),
 ('2026-11-11',json.dumps(dict(low=1,high=3,mean=2,change=0,rangeEvidence='page',fetchedAt='new')))),
 ('cci','cci_quotes(day TEXT,grade TEXT,payload TEXT)','INSERT INTO cci_quotes VALUES (?,?,?)',
 ('2026-11-11','CCI5500',json.dumps(dict(price=1000,change=-1,checked_at='old'))),
 ('2026-11-11','CCI5500',json.dumps(dict(price=1000,change=-1,checked_at='new')))),
 ('valuation_ruler','points(trade_date TEXT,payload TEXT)','INSERT INTO points VALUES (?,?)',
 ('2026-11-11',json.dumps(dict(index_close='4000',mean_pe_ttm='20',pe_sum='6000',positive_count=300,loss_count=0,updated_at='old'))),
 ('2026-11-11',json.dumps(dict(index_close='4000',mean_pe_ttm='20',pe_sum='6000',positive_count=300,loss_count=0,updated_at='new')))),
 ('cash_cow','snapshots(years INTEGER,payload TEXT)','INSERT INTO snapshots VALUES (?,?)',
 (3,json.dumps(dict(items=[dict(code='000001',average_yield=5)],snapshot=dict(updated_at='old',run_id='1')))),
 (3,json.dumps(dict(items=[dict(code='000001',average_yield=5)],snapshot=dict(updated_at='new',run_id='2')))))])
def test_metadata_only_changes_never_notify(settings,task,table,rows_sql,first,second):
    path=settings.log_path/'source.sqlite'
    @contextmanager
    def connection():
        db=sqlite3.connect(path)
        try:
            with db:yield db
        finally:db.close()
    monitor=SimpleNamespace(db=connection)
    with connection() as db:db.execute('CREATE TABLE '+table);db.execute(rows_sql,first)
    before=n.published_values(monitor,task)
    with connection() as db:db.execute('DELETE FROM '+table.split('(')[0]);db.execute(rows_sql,second)
    assert before==n.published_values(monitor,task)
