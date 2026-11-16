import httpx
import pytest
from app.services import cash_cow_source as m


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(m.EastmoneyCashCowSource, '_rate_limit', lambda self: self.check_cancel())
    monkeypatch.setattr(m.time, 'sleep', lambda _: None)
    monkeypatch.setattr(m, 'SELECTOR_PAGE_SIZE', 2)


def row(code, market):
    return dict(SECUCODE=code+'.'+market, SECURITY_CODE=code, SECURITY_NAME_ABBR='Test',
                LISTING_DATE='2000-01-01', MAX_TRADE_DATE='2026-11-13')


def payload(page):
    return dict(code=0, success=True, result=dict(currentpage=page, nextpage=page == 1,
        data=[row('000001','SZ'),row('600000','SH')] if page == 1 else [row('920001','BJ')]))


def test_transport_failure_uses_complete_selector_and_caches(tmp_path):
    calls=[]
    def handler(r):
        calls.append(r)
        if r.url.host == '82.push2.eastmoney.com':
            raise httpx.RemoteProtocolError('disconnected',request=r)
        assert str(r.url).startswith(m.SELECTOR)
        return httpx.Response(200,json=payload(int(r.url.params['p'])))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source=m.EastmoneyCashCowSource(client,as_of='2026-11-16',cache_dir=tmp_path)
        assert [r['code'] for r in source.universe()]==['000001','600000','920001']
        assert source.secids=={'000001':'0.000001','600000':'1.600000','920001':'0.920001'}
        assert len(calls)==4
        assert len(source.universe())==3
        assert len(calls)==4


@pytest.mark.parametrize('status', [401,403,429])
def test_no_alternate_after_access_or_rate_denial(status):
    calls=[]
    def handler(r):
        calls.append(r)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.CashCowSourceError):
            m.EastmoneyCashCowSource(client).universe()
    assert all(r.url.host=='82.push2.eastmoney.com' for r in calls)


@pytest.mark.parametrize('failure', ['partial','duplicate','future','stale','missing_date','wrong_page','non_a','missing_market'])
def test_bad_selector_never_cached(failure,tmp_path):
    def handler(r):
        if r.url.host=='82.push2.eastmoney.com':
            raise httpx.ConnectError('disconnected',request=r)
        page=int(r.url.params['p']); p=payload(page); result=p['result']
        if page==1:
            if failure=='partial':result['data']=result['data'][:1]
            if failure=='duplicate':result['data'][1]=result['data'][0]
            if failure=='future':result['data'][0]['LISTING_DATE']='2026-11-17'
            if failure=='stale':result['data'][0]['MAX_TRADE_DATE']='2026-11-01'
            if failure=='missing_date':del result['data'][0]['LISTING_DATE']
            if failure=='wrong_page':result['currentpage']=2
            if failure=='non_a':result['data'][0]=row('200001','SZ')
            if failure=='missing_market':result['nextpage']=False
        return httpx.Response(200,json=p)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.CashCowSourceError):
            m.EastmoneyCashCowSource(client,as_of='2026-11-16',cache_dir=tmp_path).universe()
    assert not list(tmp_path.glob('*.json'))


def test_partial_primary_never_merges_into_fallback(monkeypatch):
    def fail_partial(self):
        self.secids['999999']='0.999999'
        try:
            raise httpx.ConnectError('connection lost')
        except httpx.ConnectError as exc:
            raise m.CashCowSourceError('failed') from exc
    monkeypatch.setattr(m.EastmoneyCashCowSource,'_universe_uncached',fail_partial)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json=payload(int(r.url.params['p']))))) as client:
        source=m.EastmoneyCashCowSource(client,as_of='2026-11-16')
        stocks=source.universe()
        assert {s['code'] for s in stocks}==set(source.secids)=={'000001','600000','920001'}
