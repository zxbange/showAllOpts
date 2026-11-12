from datetime import date, datetime, timezone
import json
import httpx
import pytest
from app.core.config import Settings
from app.services.cci_coal import CCICoal, latest_slot
from app.services.cci_source import CCIError, GRADES, fetch_rows, parse_response, validate_rows


def quote(day='2026-11-02', grade='CCI5500', price=1015, change=None):
    return dict(quoteDate=day, grade=grade, price=price, change=change)


def response(day='2026-11-02', grade='CCI5500', **fields):
    data = dict(date=day, index_type='日指数', index_name=grade, price=1015, change=None, unit='元/吨')
    data.update(fields)
    return dict(rows=[dict(kind='index', data=data)], total=1)


def test_seed_provenance_gaps_and_restart(tmp_path):
    store = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    data = store.history()
    assert len(data['rows']) == 586 and data['missingChanges'] == 238
    assert len({r['quoteDate'] for r in data['rows']}) == 197
    assert [sum(r['grade'] == g for r in data['rows']) for g in GRADES] == [194,195,197]
    assert not any(r['quoteDate'] == '2026-02-24' for r in data['rows'])
    assert not any(r['quoteDate'] == '2026-01-21' and r['grade'] == 'CCI5000' for r in data['rows'])
    assert all(r['official'] is False and r['evidence'] == 'public_query' for r in data['rows'])
    assert not {'open','close','high','low','volume'} & data['rows'][0].keys()
    assert CCICoal(Settings(_env_file=None,log_dir=tmp_path)).history()['rows'] == data['rows']
    assert data['status']['schedule'] == '21:00'
    assert not (store.root / '../commodities/history.sqlite3').exists()


@pytest.mark.parametrize('instant,expected', [
    ('2026-11-07T02:59:59+00:00','2026-11-06T21:00:00+08:00'),
    ('2026-11-07T03:00:00+00:00','2026-11-06T21:00:00+08:00'),
    ('2026-11-07T09:59:59+00:00','2026-11-06T21:00:00+08:00'),
    ('2026-11-07T10:00:00+00:00','2026-11-06T21:00:00+08:00'),
    ('2026-11-07T12:59:59+00:00','2026-11-06T21:00:00+08:00'),
    ('2026-11-07T13:00:00+00:00','2026-11-07T21:00:00+08:00'),
    ('2027-01-01T00:00:00+00:00','2026-12-31T21:00:00+08:00'),
])
def test_only_shanghai_21_slot(instant, expected):
    assert latest_slot(datetime.fromisoformat(instant)).isoformat() == expected


@pytest.mark.parametrize('field,value', [('unit','美元/吨'),('index_name','CCI进口5500'),('index_type','周指数'),('price',True),('price','NaN'),('price',0),('date','2026-11-03')])
def test_strict_response(field,value):
    with pytest.raises(CCIError):
        parse_response(response(**{field:value}), 'CCI5500','2026-11-01','2026-11-02')


def test_null_is_not_zero_and_conflicts_rejected():
    rows = parse_response(response(change=None),'CCI5500','2026-11-01','2026-11-02')
    assert rows[0]['change'] is None
    assert parse_response(response(change=0),'CCI5500','2026-11-01','2026-11-02')[0]['change'] == 0
    assert parse_response(response(price='1015.5',change='-2'),'CCI5500','2026-11-01','2026-11-02')[0]['price'] == 1015.5
    with pytest.raises(CCIError):validate_rows([quote(),quote(price=999)])
    payload = response();payload['total'] = 1001
    with pytest.raises(CCIError):parse_response(payload,'CCI5500','2026-11-01','2026-11-02')
    with pytest.raises(CCIError):validate_rows([quote(day='2025-12-31')])


def test_public_requests_are_windowed_and_bounded():
    requests = []
    def handle(request):
        assert str(request.url).startswith('https://coalindex.com/api/market?')
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        p = request.url.params
        requests.append(p)
        assert p['tab'] == 'steamcoal-index'
        assert p['date_to'] == p['date_from'] == '2026-11-03'
        return httpx.Response(200,json=response(day=p['date_from'],grade=p['index_name']))
    with httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=False) as client:
        rows=fetch_rows(client,'2026-11-03','2026-11-03')
    assert len(requests)==3 and len(rows)==3
    assert {p['index_name'] for p in requests}==set(GRADES)


@pytest.mark.parametrize('status',[302,401,403,429,500])
def test_no_redirect_or_access_bypass(status):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(status,headers={'Location':'https://other.invalid/private'})
    with httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=False) as client:
        details=[]
        assert fetch_rows(client,'2026-11-03','2026-11-03',diagnostics=details)==[]
    assert len(calls)==3
    assert all(d['error']==f'HTTP {status}' for d in details)


def test_failed_empty_and_repeated_collection_preserve_history(tmp_path,monkeypatch):
    import app.services.cci_coal as module
    monkeypatch.setattr(module,'now',lambda:datetime(2026,11,3,12,tzinfo=timezone.utc))
    store=CCICoal(Settings(_env_file=None,log_dir=tmp_path));old=store.history()['rows']
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[])
    store.collect(store.lock());assert store.history()['rows']==old
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[quote(price=999),quote(day='bad')])
    store.collect(store.lock());assert store.history()['rows']==old
    assert store.status()['error']
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[quote(day='2026-11-03',price=999,change=None)])
    store.collect(store.lock());first=store.history()['rows']
    store.collect(store.lock());assert store.history()['rows']==first and len(first)==587
    assert next(r for r in first if r['quoteDate']=='2026-11-03' and r['grade']=='CCI5500')['price']==999
    assert CCICoal(Settings(_env_file=None,log_dir=tmp_path)).history()['rows']==first


def test_independent_locks_and_durable_slots(tmp_path,monkeypatch):
    import app.services.cci_coal as module
    import app.services.commodities as commodities
    from app.services.commodities import Commodities
    # Freeze both clocks before constructing stores: refresh() intentionally rejects
    # slots older than their initialization, so a fixed slot with wall-clock setup
    # becomes invalid as soon as the real date passes it.
    current = [datetime(2026,11,8,9,59,tzinfo=timezone.utc)]
    monkeypatch.setattr(module, 'now', lambda: current[0])
    monkeypatch.setattr(commodities, 'now', lambda: current[0])
    store=CCICoal(Settings(_env_file=None,log_dir=tmp_path))
    a00=Commodities(Settings(_env_file=None,log_dir=tmp_path))
    old=a00.history()['rows']
    lock=a00.lock()
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[])
    try:
        assert not store.refresh(slot=latest_slot(current[0]))
        assert store.thread is None
        current[0] = datetime(2026,11,8,13,tzinfo=timezone.utc)
        slot=latest_slot(current[0])
        assert store.refresh(slot=slot)
        store.thread.join(5)
        assert not store.thread.is_alive()
        assert not CCICoal(Settings(_env_file=None,log_dir=tmp_path)).refresh(slot=slot)
    finally:lock.release()
    assert a00.history()['rows']==old
    assert a00.slot_for(datetime(2026,11,8,3,tzinfo=timezone.utc)).hour==11


def test_api_read_auth_and_admin_write(tmp_path):
    from fastapi import FastAPI,Depends
    from fastapi.testclient import TestClient
    from app.api.routes.commodities import router
    from app.api.routes.accounts import require_user
    from account_helpers import install_accounts,sign_in,PASSWORD
    app=FastAPI();settings=Settings(_env_file=None,log_dir=tmp_path,auth_allow_http=True)
    install_accounts(app,settings);app.state.cci_coal=CCICoal(settings)
    app.include_router(router,prefix='/api/v1',dependencies=[Depends(require_user)])
    with TestClient(app) as client:
        assert client.get('/api/v1/commodities/cci').status_code==401
        app.state.accounts.create('viewer',PASSWORD,'viewer',app.state.accounts.list()[0]['id'])
        sign_in(client,'viewer')
        assert client.get('/api/v1/commodities/cci').status_code==200
        assert client.post('/api/v1/commodities/cci/refresh').status_code==403
        sign_in(client);client.headers['X-CSRF-Token']='bad'
        assert client.post('/api/v1/commodities/cci/refresh').status_code==403

@pytest.mark.parametrize('mode,expected,hits', [('full','completed',3),('empty','empty',0),('partial','partial',1),('network','failed',0),('parse','failed',0),('stale','failed',0)])
def test_today_results_history_and_diagnostics(tmp_path, monkeypatch, caplog, mode, expected, hits):
    import app.services.cci_coal as module
    real_client = httpx.Client
    # UTC is still Nov 2; Shanghai's target must already be Nov 3.
    monkeypatch.setattr(module, 'now', lambda: datetime(2026,11,2,16,1,tzinfo=timezone.utc))
    calls=[]
    def handle(request):
        p=request.url.params; calls.append(p)
        assert p['date_from']==p['date_to']=='2026-11-03'
        if mode=='network':raise httpx.ConnectError('secret-token https://private.invalid',request=request)
        if mode=='parse':return httpx.Response(200,text='private-invalid-json')
        if mode=='empty' or (mode=='partial' and p['index_name']!='CCI5500'):
            return httpx.Response(200,json=dict(rows=[],total=0))
        return httpx.Response(200,json=response(day='2026-11-02' if mode=='stale' else '2026-11-03',grade=p['index_name']))
    monkeypatch.setattr(module.httpx,'Client',lambda **kwargs:real_client(transport=httpx.MockTransport(handle),**kwargs))
    store=CCICoal(Settings(_env_file=None,log_dir=tmp_path));old=store.history()['rows']
    with caplog.at_level('INFO'):
        store.collect(store.lock())
    state=store.status()
    assert state['outcome']==expected and state['observed']==hits
    assert state['inserted']==hits and state['changed']==0
    assert len(calls)==3 and len(state['grades'])==3
    assert [r for r in store.history()['rows'] if r['quoteDate']<'2026-11-03']==old
    assert 'secret-token' not in caplog.text and 'private-invalid' not in caplog.text
    assert all('elapsedMs' in d and 'dateFrom' in d for d in state['grades'])
    if mode=='stale':assert all(d['latestQuoteDate']=='2026-11-02' and d['targetHits']==0 for d in state['grades'])
    store.collect(store.lock())
    assert store.status()['inserted']==store.status()['changed']==0
    assert len(store.history()['rows'])==len(old)+hits


def test_reject_oversized_history_window():
    with httpx.Client(trust_env=False) as client, pytest.raises(CCIError):
        fetch_rows(client,'2026-01-01','2026-11-03')


def test_partial_network_failure_and_changed_counts(tmp_path, monkeypatch):
    import app.services.cci_coal as module
    real_client=httpx.Client
    monkeypatch.setattr(module,'now',lambda:datetime(2026,11,3,12,tzinfo=timezone.utc))
    value=[1015]
    def handle(request):
        grade=request.url.params['index_name']
        if grade=='CCI5000':raise httpx.ReadTimeout('sensitive',request=request)
        return httpx.Response(200,json=response(day='2026-11-03',grade=grade,price=value[0]))
    monkeypatch.setattr(module.httpx,'Client',lambda **kwargs:real_client(transport=httpx.MockTransport(handle),**kwargs))
    store=CCICoal(Settings(_env_file=None,log_dir=tmp_path));old=store.history()['rows']
    store.collect(store.lock())
    assert store.status()['outcome']=='partial' and store.status()['inserted']==2
    assert store.status()['grades'][1]['error']=='请求超时'
    value[0]=1016
    store.collect(store.lock())
    assert store.status()['changed']==2 and store.status()['inserted']==0
    assert sum(g['changed'] for g in store.status()['grades'])==2
    assert [r for r in store.history()['rows'] if r['quoteDate']<'2026-11-03']==old
    store.stopping.set()
    store.collect(store.lock())
    assert store.status()['outcome']=='interrupted'
    assert store.status()['running'] is False
    assert len(store.history()['rows'])==len(old)+2


@pytest.mark.parametrize('initialized, resumed', [
    ('2026-11-04T09:00:00+00:00', '2026-11-04T16:01:00+00:00'),
    ('2026-11-04T09:00:00+00:00', '2026-11-05T09:59:00+00:00'),
    ('2026-12-31T09:00:00+00:00', '2026-12-31T16:01:00+00:00'),
])
def test_expired_slot_is_skipped_without_querying_today(tmp_path, monkeypatch, initialized, resumed):
    import app.services.cci_coal as module
    import app.services.commodities as commodities
    current = [datetime.fromisoformat(initialized)]
    monkeypatch.setattr(module, 'now', lambda: current[0])
    monkeypatch.setattr(commodities, 'now', lambda: current[0])
    settings = Settings(_env_file=None, log_dir=tmp_path)
    store = CCICoal(settings)
    original = store.history()['rows']
    requests = []

    def fetch(client, start, end, **kwargs):
        requests.append((start, end))
        return [quote(day=end, grade=grade) for grade in kwargs['grades']]

    monkeypatch.setattr(module, 'fetch_rows', fetch)
    current[0] = datetime.fromisoformat(resumed)
    slot = latest_slot(current[0])
    assert store.refresh(slot=slot)
    store.thread.join(5)
    assert not store.thread.is_alive()
    assert requests == []
    assert store.history()['rows'] == original
    state = store.status()
    assert state['outcome'] == 'skipped'
    assert state['targetDate'] == slot.date().isoformat()
    assert state['slot'] == slot.isoformat()
    assert state['observed'] == state['inserted'] == state['changed'] == 0
    assert state['grades'] == [] and state['running'] is False
    assert state['skippedAt'] and not state.get('checkedAt')
    with store.db() as db:
        assert db.execute('SELECT status,finished_at FROM schedule_runs WHERE slot=?',
                          (slot.isoformat(),)).fetchone() == ('skipped', current[0].isoformat())
    restarted = CCICoal(settings)
    assert not restarted.refresh(slot=slot)
    assert requests == []
    current[0] = current[0].astimezone(commodities.CHINA).replace(hour=21, minute=0)
    today_slot = latest_slot(current[0])
    assert restarted.refresh(slot=today_slot)
    restarted.thread.join(5)
    assert not restarted.thread.is_alive()
    assert requests[0] == (today_slot.date().isoformat(), today_slot.date().isoformat())
    assert len(requests) > 1  # The 21:00 update now also recovers the missed interval.
    assert all(start >= '2026-11-02' and end <= today_slot.date().isoformat() for start, end in requests)
    assert restarted.status()['outcome'] == 'completed'
    assert restarted.status()['targetDate'] == today_slot.date().isoformat()
    assert restarted.status()['slot'] == today_slot.isoformat()
    assert len(restarted.history()['rows']) > len(original) + 3
    assert restarted.status()['recovery']['checkedCount'] > 3


def test_slot_delayed_across_shanghai_midnight_is_skipped(tmp_path, monkeypatch):
    import app.services.cci_coal as module
    import app.services.commodities as commodities
    current = [datetime(2026, 11, 4, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(module, 'now', lambda: current[0])
    monkeypatch.setattr(commodities, 'now', lambda: current[0])
    store = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    original = store.history()['rows']
    requests = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: requests.append(a) or [])

    class DeferredThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            pass

        def run(self):
            self.target(*self.args)

    monkeypatch.setattr(commodities.threading, 'Thread', DeferredThread)
    current[0] = datetime(2026, 11, 4, 15, 59, tzinfo=timezone.utc)
    slot = latest_slot(current[0])
    assert store.refresh(slot=slot)
    current[0] = datetime(2026, 11, 4, 16, 1, tzinfo=timezone.utc)
    store.thread.run()
    assert requests == []
    assert store.status()['outcome'] == 'skipped'
    assert store.status()['targetDate'] == '2026-11-04'
    assert store.status()['running'] is False
    assert store.history()['rows'] == original


def cci_scheduled_store(tmp_path, monkeypatch):
    import app.services.cci_coal as module
    import app.services.commodities as commodities
    current = [datetime(2026, 11, 4, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(module, 'now', lambda: current[0])
    monkeypatch.setattr(commodities, 'now', lambda: current[0])
    store = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    # These scheduler tests isolate daily claims/retries from recovery planning,
    # which is covered with real window responses in test_cci_recovery.py.
    with store.db() as db:
        db.executemany('INSERT INTO cci_recovery VALUES(?,?,?)',
                       [(grade, '2026-11-04', 'installation_date') for grade in GRADES])
    return module, current, store


def run_scheduled(store, moment):
    assert store.refresh(slot=latest_slot(moment))
    store.thread.join(5)
    assert not store.thread.is_alive()


def test_same_day_retries_wait_and_stop_when_complete(tmp_path, monkeypatch):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    requests = []

    def fetch(client, start, end, **kwargs):
        requests.append((start, end))
        if len(requests) == 1:
            return []
        grades = GRADES[:1] if len(requests) == 2 else GRADES
        return [quote(day=start, grade=grade) for grade in grades]

    monkeypatch.setattr(module, 'fetch_rows', fetch)
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert store.status()['outcome'] == 'empty'
    current[0] = datetime(2026, 11, 4, 13, 29, 59, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    current[0] = datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert store.status()['outcome'] == 'partial'
    current[0] = datetime(2026, 11, 4, 14, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert store.status()['outcome'] == 'completed'
    assert store.status()['inserted'] == 2 and store.status()['changed'] == 0
    current[0] = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    restarted = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    assert not restarted.refresh(slot=latest_slot(current[0]))
    assert requests == [('2026-11-04', '2026-11-04')] * 3
    with store.db() as db:
        assert [row[0] for row in db.execute('SELECT status FROM schedule_runs ORDER BY slot')] == ['empty', 'partial', 'completed']


@pytest.mark.parametrize('outcome', ['empty', 'failed'])
def test_same_day_retry_quota_persists_and_resets_next_day(tmp_path, monkeypatch, outcome):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    original = store.history()['rows']
    requests = []

    def fetch(client, start, end, **kwargs):
        requests.append(start)
        if outcome == 'failed':
            raise httpx.ConnectError('test network failure')
        return []

    monkeypatch.setattr(module, 'fetch_rows', fetch)
    for minute in (0, 30, 60, 90):
        current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc) + module.timedelta(minutes=minute)
        run_scheduled(store, current[0])
        assert store.status()['outcome'] == outcome
        store = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    current[0] = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    current[0] = datetime(2026, 11, 4, 16, 1, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert requests == ['2026-11-04'] * 4
    assert store.history()['rows'] == original
    current[0] = datetime(2026, 11, 5, 13, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert requests[4] == '2026-11-05'
    # Missing Nov 4 is now eligible for recovery on Nov 5, without new daily slots.
    assert len(requests) == (8 if outcome == 'empty' else 5)


def test_retry_deadline_is_shanghai_midnight(tmp_path, monkeypatch):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    current[0] = datetime(2026, 11, 4, 15, 45, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    current[0] = datetime(2026, 11, 4, 15, 59, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    current[0] = datetime(2026, 11, 4, 16, 15, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert calls == [('2026-11-04', '2026-11-04')]


def test_retry_recovers_abandoned_claim_after_delay(tmp_path, monkeypatch):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    slot = latest_slot(current[0])
    with store.db() as db:
        db.execute('INSERT INTO schedule_runs(slot,status) VALUES(?,?)', (slot.isoformat(), 'running'))
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    current[0] = datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)
    assert not store.refresh(slot=slot)
    assert store.status()['outcome'] == 'interrupted'
    assert calls == []
    current[0] = datetime(2026, 11, 4, 14, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert calls == [('2026-11-04', '2026-11-04')]


@pytest.mark.parametrize('legacy', [False, True])
def test_retry_claim_is_exclusive_between_instances(tmp_path, monkeypatch, legacy):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: [])
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    if legacy:
        legacy_run(store)
    else:
        run_scheduled(store, current[0])
    second = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    current[0] = datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)
    calls, release, entered = [], threading.Event(), threading.Event()

    def fetch(client, start, end, **kwargs):
        calls.append((start, end))
        entered.set()
        assert release.wait(5)
        return [quote(day=start, grade=grade) for grade in GRADES]

    monkeypatch.setattr(module, 'fetch_rows', fetch)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(s.refresh, slot=latest_slot(current[0])) for s in (store, second)]
            assert sorted(f.result(timeout=5) for f in futures) == [False, True]
        assert entered.wait(5)
        assert len(calls) == 1
    finally:
        release.set()
        for instance in (store, second):
            if instance.thread:
                instance.thread.join(5)
                assert not instance.thread.is_alive()
    assert store.status()['outcome'] == 'completed'
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM schedule_runs').fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM cci_quotes WHERE day='2026-11-04'").fetchone()[0] == 3


def legacy_run(store, *, slot='2026-11-04T18:00:00+08:00', result='empty',
               finished='2026-11-04T10:00:00+00:00'):
    with store.db() as db:
        db.execute('INSERT INTO schedule_runs(slot,status,finished_at) VALUES(?,?,?)',
                   (slot, result, finished))


@pytest.mark.parametrize('stamp', ['2026-11-04T18:00:00+08:00', '2026-11-04T10:00:00+00:00'])
def test_21_upgrade_keeps_completed_18_attempt_without_repeat(tmp_path, monkeypatch, stamp):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    legacy_run(store, slot=stamp, result='completed')
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a) or [])
    for hour in (13, 14, 15):
        current[0] = datetime(2026, 11, 4, hour, tzinfo=timezone.utc)
        assert not store.refresh(slot=latest_slot(current[0]))
    assert calls == []
    with store.db() as db:
        assert db.execute('SELECT slot,status,finished_at FROM schedule_runs').fetchall() == [
            (stamp, 'completed', '2026-11-04T10:00:00+00:00')]


@pytest.mark.parametrize('result', ['empty', 'partial', 'failed', 'interrupted'])
def test_21_upgrade_waits_until_21_and_normalizes_retry_slot(tmp_path, monkeypatch, result):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    legacy_run(store, result=result)
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    current[0] = datetime(2026, 11, 4, 10, 30, tzinfo=timezone.utc)
    # Even an old scheduler's explicit 18:00 slot cannot start an early retry.
    assert not store.refresh(slot=datetime.fromisoformat('2026-11-04T18:00:00+08:00'))
    current[0] = datetime(2026, 11, 4, 12, 59, 59, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert calls == []
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert store.status()['slot'] == '2026-11-04T21:00:00+08:00'
    assert calls == [('2026-11-04', '2026-11-04')]
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM schedule_runs').fetchone()[0] == 2


def test_21_upgrade_waits_30_minutes_after_late_legacy_finish(tmp_path, monkeypatch):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    legacy_run(store, slot='2026-11-04T10:00:00+00:00', finished='2026-11-04T12:50:00+00:00')
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    current[0] = datetime(2026, 11, 4, 13, 19, 59, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    current[0] = datetime(2026, 11, 4, 13, 20, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert calls == [('2026-11-04', '2026-11-04')]
    assert store.status()['slot'] == '2026-11-04T21:20:00+08:00'


@pytest.mark.parametrize('recover_at', ['2026-11-04T12:50:00+00:00', '2026-11-04T13:00:00+00:00'])
def test_21_upgrade_recovers_old_running_once_and_waits(tmp_path, monkeypatch, recover_at):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    legacy_run(store, result='running', finished=None)
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    current[0] = datetime.fromisoformat(recover_at)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert store.status()['outcome'] == 'interrupted'
    with store.db() as db:
        assert db.execute('SELECT status,finished_at FROM schedule_runs').fetchall() == [('interrupted', recover_at)]
    current[0] += module.timedelta(minutes=29, seconds=59)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert calls == []
    current[0] += module.timedelta(seconds=1)
    run_scheduled(store, current[0])
    assert calls == [('2026-11-04', '2026-11-04')]
    assert store.status()['slot'] == current[0].astimezone(module.CHINA).isoformat()


def test_21_upgrade_does_not_interrupt_a_live_old_worker(tmp_path, monkeypatch):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    legacy_run(store, result='running', finished=None)
    second = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a) or [])
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    lock = store.lock()
    try:
        assert not second.refresh(slot=latest_slot(current[0]))
        with store.db() as db:
            assert db.execute('SELECT status,finished_at FROM schedule_runs').fetchall() == [('running', None)]
            # The old worker finishes successfully while still holding its lock.
            db.execute("UPDATE schedule_runs SET status='completed',finished_at=?", (current[0].isoformat(),))
    finally:
        lock.release()
    assert not second.refresh(slot=latest_slot(current[0]))
    assert calls == []


@pytest.mark.parametrize('legacy_count', [2, 4])
def test_21_upgrade_counts_old_attempts_in_daily_budget(tmp_path, monkeypatch, legacy_count):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    for i in range(legacy_count):
        stamp = datetime(2026, 11, 4, 10, tzinfo=timezone.utc) + module.timedelta(minutes=30*i)
        legacy_run(store, slot=stamp.isoformat(), finished=stamp.isoformat())
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    for i in range(4 - legacy_count):
        current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc) + module.timedelta(minutes=30*i)
        run_scheduled(store, current[0])
        store = CCICoal(Settings(_env_file=None, log_dir=tmp_path))
    current[0] = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert len(calls) == 4 - legacy_count
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM schedule_runs').fetchone()[0] == 4


def test_21_schedule_rejects_ahead_scheduler_clock_but_keeps_manual_refresh(tmp_path, monkeypatch):
    import app.services.commodities as commodities
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    current[0] = datetime(2026, 11, 4, 12, 59, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(commodities, 'now', lambda: datetime(2026, 11, 4, 13, tzinfo=timezone.utc))
    assert not store.refresh(slot=store.slot_for(commodities.now()))
    assert calls == []
    assert store.refresh()
    store.thread.join(5)
    assert calls == [('2026-11-04', '2026-11-04')]
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert len(calls) == 2


@pytest.mark.parametrize('result', ['completed', 'empty', 'failed'])
def test_21_upgrade_before_21_keeps_today_result_instead_of_yesterday_skip(tmp_path, monkeypatch, result):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    # A long-running installation makes yesterday's new 21:00 slot eligible by
    # the initialization floor, even though that schedule did not exist then.
    with store.db() as db:
        db.execute("UPDATE schedule_meta SET initialized_at='2026-11-02T00:00:00+00:00'")
    legacy_run(store, result=result)
    store.state(stage='旧18点结果', outcome=result, targetDate='2026-11-04',
                checkedAt='2026-11-04T10:00:00+00:00', slot='2026-11-04T18:00:00+08:00')
    before = store.status()
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a) or [])
    current[0] = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert store.status() == before and calls == []
    with store.db() as db:
        assert db.execute('SELECT slot FROM schedule_runs').fetchall() == [('2026-11-04T18:00:00+08:00',)]


def test_21_schedule_thread_start_failure_releases_lock_and_recovers_after_delay(tmp_path, monkeypatch):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    real_thread = module.threading.Thread
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])

    class FailedThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError('test thread start failed')

    monkeypatch.setattr(module.threading, 'Thread', FailedThread)
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match='thread start failed'):
        store.refresh(slot=latest_slot(current[0]))
    assert store.status()['running'] is False and calls == []
    with store.db() as db:
        assert db.execute('SELECT status,finished_at FROM schedule_runs').fetchall() == [('running', None)]
    monkeypatch.setattr(module.threading, 'Thread', real_thread)
    assert not store.refresh(slot=latest_slot(current[0]))
    assert store.status()['outcome'] == 'interrupted'
    current[0] = datetime(2026, 11, 4, 13, 29, 59, tzinfo=timezone.utc)
    assert not store.refresh(slot=latest_slot(current[0]))
    current[0] = datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert calls == [('2026-11-04', '2026-11-04')]


def test_previous_day_last_microsecond_is_not_todays_success(tmp_path, monkeypatch):
    module, current, store = cci_scheduled_store(tmp_path, monkeypatch)
    legacy_run(store, slot='2026-11-03T23:59:59.999999+08:00', result='completed',
               finished='2026-11-03T16:00:00+00:00')
    calls = []
    monkeypatch.setattr(module, 'fetch_rows', lambda *a, **k: calls.append(a[1:]) or [])
    current[0] = datetime(2026, 11, 4, 13, tzinfo=timezone.utc)
    run_scheduled(store, current[0])
    assert calls == [('2026-11-04', '2026-11-04')]
