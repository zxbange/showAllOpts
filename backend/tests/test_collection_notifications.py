"""Both collection triggers share terminal reporting and an idempotent outbox."""
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

def test_automatic_failure_no_change_enqueues_but_inactive_and_disabled_are_silent(settings,monkeypatch):
    assert emit(settings,changed_count=0,outcome='failed')
    assert not emit(settings,run_key='skipped',outcome='skipped')
    assert not emit(settings,run_key='running',outcome='running')
    monkeypatch.setattr(n,'enabled',lambda *_:False)
    assert not emit(settings,run_key='disabled')
    assert len(rows(settings)) == 1
    assert '采集结果：失败' in rows(settings)[0]['message']

def test_persistent_dedup_partial_update_and_formatted_content(settings):
    assert emit(settings,outcome='partial')
    assert not emit(settings,outcome='partial')
    row,=rows(settings)
    assert row['status']=='pending'
    assert 'CCI 动力煤' in row['message']
    assert '采集结果：部分完成' in row['message']
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
    assert len(rows(settings)) == 2
    assert '更新记录：0' in rows(settings)[1]['message']
    m.collect(Lock(),{'run_id':'3','current_scheduled_slot':None},value=130)
    assert len(rows(settings)) == 3
    m.collect(Lock(),{'run_id':'4','current_scheduled_slot':'2026-11-12'},value=140,fail=True)
    assert '失败' in rows(settings)[3]['message']

def test_all_templates(settings):
    assert '—' in n.summarize_changes('commodities',{'2026-11-11':1},{'2026-11-11':dict(low=None,high=None,mean=21000)})[2]
    assert '近3年' in n.summarize_changes('cash_cow',{3:[]},{3:[]})[0]
    assert '创红比：0.500000' in n.summarize_changes('growth_dividend',{('2026-11-11','399006'):2500},{('2026-11-11','399006'):2500,('2026-11-11','000922'):5000})

def test_snapshot_is_taken_before_business_lock_release(settings):
    monitor=Monitor(settings)
    def competing_writer():
        with monitor.db() as db:db.execute('UPDATE points SET ratio=999')
    monitor.collect(Lock(competing_writer),{'run_id':'race','current_scheduled_slot':'2026-11-12'},value=120)
    assert '两融担保比例：120%' in rows(settings)[0]['message']
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


def test_skip_and_queue_reasons_are_observable_without_payload(settings, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=n.__name__)
    emit(settings, scheduled=False, summary=['DO_NOT_LOG_PAYLOAD'])
    emit(settings, changed_count=0)
    monkeypatch.setattr(n, 'enabled', lambda *_: False)
    emit(settings)
    monkeypatch.setattr(n, 'enabled', lambda *_: True)
    emit(settings, summary=['DO_NOT_LOG_PAYLOAD'])
    emit(settings)
    assert 'reason=disabled' in caplog.text
    assert 'notification enqueued task=cci changed_count=1' in caplog.text
    assert 'notification duplicate task=cci' in caplog.text
    assert 'DO_NOT_LOG_PAYLOAD' not in caplog.text


def test_valuation_current_store_changes_notify_and_metadata_does_not(settings):
    monitor = Monitor(settings)
    with monitor.db() as db:
        db.execute('CREATE TABLE index_points_v2(index_code TEXT, trade_date TEXT, payload TEXT)')
        payload = dict(index_close='4000', mean_pe_ttm='20', member_count=300,
                       statistics={'ttm': {'eligible': 290}}, updated_at='old')
        db.execute('INSERT INTO index_points_v2 VALUES (?,?,?)',
                   ('000300.XSHG', '2026-11-16', json.dumps(payload)))
    before = n.published_values(monitor, 'valuation_ruler')
    assert before[('2026-11-16', '000300.XSHG')]['mean_pe_ttm'] == '20'
    assert '沪深300：4000；等权平均 PE(TTM)：20' in n.summarize_changes('valuation_ruler', before, before)
    payload['updated_at'] = 'new'
    with monitor.db() as db:
        db.execute('UPDATE index_points_v2 SET payload=?', (json.dumps(payload),))
    assert before == n.published_values(monitor, 'valuation_ruler')
    payload['mean_pe_ttm'] = '21'
    with monitor.db() as db:
        db.execute('UPDATE index_points_v2 SET payload=?', (json.dumps(payload),))
    assert before != n.published_values(monitor, 'valuation_ruler')


def test_valuation_current_hook_emits_after_current_store_publish(settings):
    monitor = Monitor(settings)
    with monitor.db() as db:
        db.execute('DROP TABLE points')
        db.execute('CREATE TABLE points(trade_date TEXT, payload TEXT)')
        db.execute('CREATE TABLE index_points_v2(index_code TEXT, trade_date TEXT, payload TEXT)')
    @n.notify_collection('valuation_ruler')
    def collect(self, lock, state, slot):
        with self.db() as db:
            db.execute('INSERT INTO index_points_v2 VALUES (?,?,?)',
                       ('000016.XSHG', '2026-11-16', json.dumps(dict(index_close='3000', mean_pe_ttm='15'))))
        lock.release()
    collect(monitor, Lock(), {'run_id': 'v2'}, 'scheduled-slot')
    row, = rows(settings)
    assert '上证50：2026-11-16' in row['message']
    assert '指数 3000' in row['message'] and '滚动等权PE 15' in row['message']

@pytest.mark.parametrize('task', list(n.LABELS))
@pytest.mark.parametrize('scheduled', [True, False])
def test_every_collection_trigger_obeys_switch_change_and_dedup(settings, monkeypatch, task, scheduled):
    args = dict(scheduled=scheduled, run_key='one-run', outcome='completed', changed_count=1, summary=['changed'])
    monkeypatch.setattr(n, 'enabled', lambda *_: False)
    assert not n.emit_collection_result(settings, task, **args)
    monkeypatch.setattr(n, 'enabled', lambda *_: True)
    assert n.emit_collection_result(settings, task, **dict(args, run_key='no-change', changed_count=0, outcome='failed'))
    assert n.emit_collection_result(settings, task, **args)
    assert not n.emit_collection_result(settings, task, **args)
    assert len(rows(settings)) == 2


def test_a00_quote_delta_uses_each_previous_valid_field_not_source_change():
    after = {
        '2026-11-10': dict(low='23000.1', high=23040, mean=23020),
        '2026-11-11': dict(low=None, high=23050, mean=float('nan')),
        '2026-11-12': dict(low='23000.2', high=23045, mean=23020, change=999),
        '2026-11-13': dict(low=99999, high=99999, mean=99999),
    }
    lines = n.summarize_changes('commodities', {'2026-11-12': after['2026-11-12']}, after)
    assert '报价日期：2026-11-12' in lines
    assert '最低 23000.2(+0.1) / 最高 23045(-5) / 均价 23020(0) 元/吨' in lines


def test_cci_quote_delta_aligns_grade_and_changed_date_with_partial_backfill():
    after = {
        ('2026-11-09', 'CCI5500'): dict(price=802),
        ('2026-11-10', 'CCI5500'): dict(price=797, change=999),
        ('2026-11-11', 'CCI5500'): dict(price=1000),
        ('2026-11-08', 'CCI5000'): dict(price=700),
        ('2026-11-09', 'CCI5000'): dict(price=None),
        ('2026-11-11', 'CCI5000'): dict(price=702),
        ('2026-11-10', 'CCI4500'): dict(price=600),
    }
    changes = {key: after[key] for key in [('2026-11-10', 'CCI5500'), ('2026-11-11', 'CCI5000'), ('2026-11-10', 'CCI4500')]}
    lines = n.summarize_changes('cci', changes, after)
    assert 'CCI5500：797(-5) 元/吨（2026-11-10）' in lines
    assert 'CCI5000：702(+2) 元/吨（2026-11-11）' in lines
    assert 'CCI4500：600(—) 元/吨（2026-11-10）' in lines


def test_first_missing_and_invalid_quotes_never_invent_delta():
    after = {'2026-11-11': dict(low=None, high='Infinity', mean='21000.00')}
    lines = n.summarize_changes('commodities', after, after)
    assert '最低 — / 最高 — / 均价 21000(—) 元/吨' in lines


def test_same_batch_previous_quote_used_for_latest_change():
    after = {('2026-11-09', 'CCI5500'): dict(price=800), ('2026-11-10', 'CCI5500'): dict(price=795)}
    assert 'CCI5500：795(-5) 元/吨（2026-11-10）' in n.summarize_changes('cci', after, after)


def test_manual_same_day_values_notify_for_each_distinct_run(settings):
    monitor = Monitor(settings)
    for run, value in [('manual-1', 120), ('manual-2', 120), ('manual-3', 130)]:
        monitor.collect(Lock(), {'run_id': run}, value=value)
    messages = rows(settings)
    assert [row['run'] for row in messages] == ['manual-1', 'manual-2', 'manual-3']
    assert '120%' in messages[0]['message']
    assert '成功（无新增或变更）' in messages[1]['message']
    assert '130%' in messages[2]['message']


def test_competing_workers_enqueue_same_manual_run_once(settings):
    from concurrent.futures import ThreadPoolExecutor
    # Separate SQLite connections model separate API workers sharing the outbox.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: emit(settings, scheduled=False), range(4)))
    assert sum(results) == 1
    assert len(rows(settings)) == 1



@pytest.mark.parametrize('outcome,label', [
    ('completed', '成功（无新增或变更）'), ('partial', '部分完成'), ('failed', '失败'),
    ('cancelled', '已取消'), ('interrupted', '已中断'), ('skipped', '未采集'),
])
def test_opted_in_manual_terminal_delivery_uses_exact_outcome_and_is_deduped(settings, monkeypatch, outcome, label):
    sent = fake_social(monkeypatch, settings)
    for _ in range(2):
        emit(settings, scheduled=False, outcome=outcome, changed_count=0, summary=[])
        n.deliver_pending(settings)
    assert len(sent) == 1
    assert sent[0].splitlines()[1] == '触发方式：手动采集'
    assert '采集结果：' + label in sent[0].splitlines()
    assert '更新记录：0' in sent[0].splitlines()


def test_authoritative_partial_result_even_when_change_snapshot_cannot_be_read(settings, monkeypatch):
    from app.services.collection_outcomes import record_task
    monitor = Monitor(settings)

    @n.notify_collection('margin')
    def collect(self, lock, state):
        record_task(self.settings, 'margin', outcome='partial', scheduled=False, run_key=state['run_id'])
        lock.release()

    def unavailable(*_):
        raise RuntimeError('PRIVATE_SOURCE_EXCEPTION')
    monkeypatch.setattr(n, 'published_values', unavailable)
    collect(monitor, Lock(), {'run_id': 'partial'})
    row, = rows(settings)
    assert '采集结果：部分完成' in row['message']
    assert '更新记录：未核实' in row['message']
    assert 'PRIVATE_SOURCE_EXCEPTION' not in row['message']


def test_valuation_history_import_is_not_manual_page_collection(settings):
    monitor = Monitor(settings)
    with monitor.db() as db:
        db.execute('DROP TABLE points')
        db.execute('CREATE TABLE points(trade_date TEXT,payload TEXT)')

    @n.notify_collection('valuation_ruler')
    def collect(self, lock, state, slot=None):
        lock.release()

    collect(monitor, Lock(), {'run_id': 'joinquant', 'phase': 'joinquant'})
    assert rows(settings) == []



def test_automatic_and_manual_content_match_except_trigger_and_bound_utf16():
    args = dict(task_id='etf', outcome='partial', changed_count=0,
                summary=['沪深300：100份 / +10份', '中证500：无法核实'] + ['📈' * 500] * 15,
                data_date='2026-11-23', collected_at='2026-11-24T01:00:00+00:00',
                error_details={'code':'timeout', 'stage':'fetch', 'trade_date':'2026-11-23',
                               'mysql_errno':2013, 'message':'SECRET-RAW-TEXT'})
    auto = n.format_result(**args, manual=False).splitlines()
    manual = n.format_result(**args, manual=True).splitlines()
    assert auto[1] == '触发方式：自动采集' and manual[1] == '触发方式：手动采集'
    assert auto[:1] + auto[2:] == manual[:1] + manual[2:]
    assert '采集时间：2026-11-24T09:00:00+08:00' in auto
    assert '目标数据日期：2026-11-23' in auto
    assert any('操作超时' in item and 'MySQL 2013' in item for item in auto)
    assert any('沪深300' in item for item in auto)
    assert all('SECRET-RAW-TEXT' not in item for item in auto)
    assert auto[-1] == '其余项目请在页面查看。'
    assert len('\n'.join(auto).encode('utf-16-le')) // 2 <= n.MAX_TELEGRAM_UNITS


def test_scheduled_delivery_zero_change_and_dedup(settings, monkeypatch):
    sent = fake_social(monkeypatch, settings)
    assert emit(settings, outcome='completed', changed_count=0)
    n.deliver_pending(settings)
    assert not emit(settings, outcome='completed', changed_count=0)
    n.deliver_pending(settings)
    assert len(sent) == 1
    assert '触发方式：自动采集' in sent[0] and '成功（无新增或变更）' in sent[0]
