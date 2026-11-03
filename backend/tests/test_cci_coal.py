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
    assert data['status']['schedule'] == '每天 18:00 · 北京时间'
    assert not (store.root / '../commodities/history.sqlite3').exists()


@pytest.mark.parametrize('instant,expected', [
    ('2026-11-07T02:59:59+00:00','2026-11-06T18:00:00+08:00'),
    ('2026-11-07T03:00:00+00:00','2026-11-06T18:00:00+08:00'),
    ('2026-11-07T09:59:59+00:00','2026-11-06T18:00:00+08:00'),
    ('2026-11-07T10:00:00+00:00','2026-11-07T18:00:00+08:00'),
    ('2027-01-01T00:00:00+00:00','2026-12-31T18:00:00+08:00'),
])
def test_only_shanghai_18_slot(instant, expected):
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
        assert (date.fromisoformat(p['date_to'])-date.fromisoformat(p['date_from'])).days <= 30
        return httpx.Response(200,json=response(day=p['date_from'],grade=p['index_name']))
    with httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=False) as client:
        rows=fetch_rows(client,'2026-01-01','2026-03-01')
    assert len(requests)==6 and len(rows)==6
    assert {p['index_name'] for p in requests}==set(GRADES)


@pytest.mark.parametrize('status',[302,401,403,429,500])
def test_no_redirect_or_access_bypass(status):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(status,headers={'Location':'https://other.invalid/private'})
    with httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=False) as client:
        with pytest.raises(httpx.HTTPStatusError):fetch_rows(client,'2026-01-01','2026-01-02')
    assert len(calls)==1


def test_failed_empty_and_repeated_collection_preserve_history(tmp_path,monkeypatch):
    import app.services.cci_coal as module
    store=CCICoal(Settings(_env_file=None,log_dir=tmp_path));old=store.history()['rows']
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[])
    store.collect(store.lock());assert store.history()['rows']==old
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[quote(price=999),quote(day='bad')])
    store.collect(store.lock());assert store.history()['rows']==old
    assert store.status()['error']
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[quote(price=999,change=None)])
    store.collect(store.lock());first=store.history()['rows']
    store.collect(store.lock());assert store.history()['rows']==first and len(first)==586
    assert next(r for r in first if r['quoteDate']=='2026-11-02' and r['grade']=='CCI5500')['price']==999
    assert CCICoal(Settings(_env_file=None,log_dir=tmp_path)).history()['rows']==first


def test_independent_locks_and_durable_slots(tmp_path,monkeypatch):
    import app.services.cci_coal as module
    from app.services.commodities import Commodities
    store=CCICoal(Settings(_env_file=None,log_dir=tmp_path))
    a00=Commodities(Settings(_env_file=None,log_dir=tmp_path))
    old=a00.history()['rows']
    lock=a00.lock()
    monkeypatch.setattr(module,'fetch_rows',lambda *a,**k:[])
    slot=latest_slot(datetime(2026,11,8,10,tzinfo=timezone.utc))
    try:
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
