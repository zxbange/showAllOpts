"""Reproduce the production 302 without allowing unvalidated network hops."""
import httpx
import pytest

from app.services import cash_cow_source as m


@pytest.fixture(autouse=True)
def no_request_delay(monkeypatch):
    monkeypatch.setattr(m.EastmoneyCashCowSource, '_rate_limit', lambda self: self.check_cancel())
    monkeypatch.setattr(m.time, 'sleep', lambda _: None)


def delayed(request):
    return str(request.url).replace('https://82.push2.', '//82.push2delay.')


def test_production_scheme_relative_redirect_preserves_all_universe_pages(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.url)
        if request.url.host == '82.push2.eastmoney.com':
            return httpx.Response(302, headers={'Location': delayed(request)})
        page = int(request.url.params['pn'])
        assert request.url.scheme == 'https'
        assert request.url.params['pz'] == '100'
        assert request.url.params['fs'] == 'm:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048'
        assert request.url.params['fields'] == 'f12,f13,f14,f26'
        rows = [dict(f12=f'{i:06}', f13=0, f14=f'股票{i}')
                for i in (range(100) if page == 1 else [100])]
        return httpx.Response(200, json=dict(rc=0, data=dict(total=101, diff=rows)))
    # Even a client configured to follow redirects must be constrained by the adapter.
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        source = m.EastmoneyCashCowSource(client, cache_dir=tmp_path)
        rows = source.universe()
    assert len(rows) == len({row['code'] for row in rows}) == 101
    assert [u.host for u in calls] == ['82.push2.eastmoney.com', '82.push2delay.eastmoney.com'] * 2
    assert [u.params['pn'] for u in calls] == ['1', '1', '2', '2']
    assert len(list(tmp_path.glob('*.json'))) == 1


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('url,host', [
    (m.UNIVERSE, '82.push2delay.eastmoney.com'),
    (m.PRICES, 'push2hisdelay.eastmoney.com'),
    (m.DATACENTER, 'datacenter-web.eastmoney.com'),
    ('https://datacenter.eastmoney.com/securities/api/data/v1/get', 'datacenter.eastmoney.com'),
])
def test_allowed_redirects_all_source_endpoints(status, url, host):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            # Reordering or percent-encoding query values is harmless.
            return httpx.Response(status, headers={'Location':
                f'https://{host}{request.url.path}?b=x%20y&a=1'})
        return httpx.Response(200, json={'complete': True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert m.EastmoneyCashCowSource(client)._get(url, {'a': 1, 'b': 'x y'}) == {'complete': True}
    assert len(calls) == 2


@pytest.mark.parametrize('location', [
    'https://attacker.example/api/qt/clist/get?pn=1',
    '//82.push2delay.eastmoney.com.attacker.example/api/qt/clist/get?pn=1',
    'http://82.push2delay.eastmoney.com/api/qt/clist/get?pn=1',
    '//127.0.0.1/api/qt/clist/get?pn=1',
    '//[::1]/api/qt/clist/get?pn=1',
    '//unknown.eastmoney.com/api/qt/clist/get?pn=1',
    '//82.push2delay.eastmoney.com:8443/api/qt/clist/get?pn=1',
    '//user:password@82.push2delay.eastmoney.com/api/qt/clist/get?pn=1',
    '//@82.push2delay.eastmoney.com/api/qt/clist/get?pn=1',
    '//82.push2delay.eastmoney.com/login?pn=1',
    '//82.push2delay.eastmoney.com/api/qt/clist/get',
    '//82.push2delay.eastmoney.com/api/qt/clist/get?pn=2',
    '//82.push2delay.eastmoney.com/api/qt/clist/get?pn=1&extra=1',
    '//82.push2delay.eastmoney.com/api/qt/clist/get?pn=1&pn=1',
    '//82.push2delay.eastmoney.com/api/qt/clist/get?pn=1#fragment',
    'https://[invalid',
    '',
    None,
])
def test_disallowed_redirect_is_never_requested_or_retried(location):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': location} if location is not None else {})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(m.CashCowSourceError, match='重定向'):
            m.EastmoneyCashCowSource(client)._get(m.UNIVERSE, {'pn': 1})
    assert len(calls) == 1


def test_unknown_original_host_cannot_authorize_a_redirect():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            302, headers={'Location': str(request.url).replace('unknown', '82.push2delay')}))) as client:
        with pytest.raises(m.CashCowSourceError, match='不受信任'):
            m.EastmoneyCashCowSource(client)._get('https://unknown.eastmoney.com/api/qt/clist/get', {'pn': 1})


def test_invalid_redirect_url_fails_closed():
    original = httpx.URL(m.UNIVERSE, params={'pn': 1})
    with pytest.raises(m.CashCowSourceError, match='不受信任'):
        m.EastmoneyCashCowSource._redirect_target(original, original,
            '//82.push2delay.eastmoney.com:notaport/api/qt/clist/get?pn=1')


def test_loop_stops_without_fetching_same_url_twice():
    calls = []
    def handler(request):
        calls.append(request)
        location = delayed(request) if len(calls) == 1 else str(request.url).replace('push2delay', 'push2')
        return httpx.Response(302, headers={'Location': location})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.CashCowSourceError, match='循环'):
            m.EastmoneyCashCowSource(client)._get(m.UNIVERSE, {'pn': 1})
    assert len(calls) == 2


@pytest.mark.parametrize('redirects,success', [(3, True), (4, False)])
def test_redirect_hop_limit(redirects, success):
    permutations = ['b=2&a=1&c=3', 'b=2&c=3&a=1', 'c=3&a=1&b=2', 'c=3&b=2&a=1']
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) <= redirects:
            return httpx.Response(302, headers={'Location': '?' + permutations[len(calls) - 1]})
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = m.EastmoneyCashCowSource(client)
        if success:
            assert source._get(m.UNIVERSE, {'a': 1, 'b': 2, 'c': 3}) == {}
        else:
            with pytest.raises(m.CashCowSourceError, match='次数过多'):
                source._get(m.UNIVERSE, {'a': 1, 'b': 2, 'c': 3})
    assert len(calls) == 4


@pytest.mark.parametrize('status,expected_calls', [(401, 2), (403, 2), (429, 4), (503, 4)])
def test_final_status_retry_policy_is_unchanged(status, expected_calls):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == '82.push2.eastmoney.com':
            return httpx.Response(302, headers={'Location': delayed(request)})
        return httpx.Response(status, json={})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.CashCowSourceError) as exc:
            m.EastmoneyCashCowSource(client)._get(m.UNIVERSE, {'pn': 1})
    assert isinstance(exc.value.__cause__, httpx.HTTPStatusError)
    assert len(calls) == expected_calls


def test_timeout_after_redirect_retries_once():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == '82.push2.eastmoney.com':
            return httpx.Response(302, headers={'Location': delayed(request)})
        raise httpx.ReadTimeout('timeout', request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.CashCowSourceError):
            m.EastmoneyCashCowSource(client)._get(m.UNIVERSE, {})
    assert len(calls) == 4


def test_cancel_between_hops_does_not_fetch_target():
    class Cancelled(Exception):
        pass
    calls = []
    def cancel():
        if calls:
            raise Cancelled()
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': delayed(request)})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Cancelled):
            m.EastmoneyCashCowSource(client, check_cancel=cancel)._get(m.UNIVERSE, {})
    assert len(calls) == 1


@pytest.mark.parametrize('payload,error', [(b'x' * 129, '过大'), (b'not-json', '读取失败'), (b'[]', '格式异常')])
def test_redirected_content_validation_and_stream_close(monkeypatch, payload, error):
    monkeypatch.setattr(m, 'MAX_BYTES', 128)
    calls, closed, read_redirect = [], [], []
    class Body(httpx.SyncByteStream):
        def __init__(self, redirect):
            self.redirect = redirect
        def __iter__(self):
            if self.redirect:
                read_redirect.append(True)
            yield payload
        def close(self):
            closed.append(self.redirect)
    def handler(request):
        calls.append(request)
        redirect = len(calls) == 1
        return httpx.Response(302 if redirect else 200, stream=Body(redirect),
                              headers={'Location': delayed(request)} if redirect else {})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.CashCowSourceError, match=error):
            m.EastmoneyCashCowSource(client)._get(m.UNIVERSE, {})
    assert len(calls) == 2
    assert closed == [True, False]
    assert read_redirect == []


@pytest.mark.parametrize('bad_data', [
    dict(total=20001, diff=[]),
    dict(total=101, diff=[dict(f12='600900', f13=1, f14='长江电力')]),
    dict(total=2, diff=[dict(f12='600900', f13=1, f14='长江电力')] * 2),
])
def test_redirect_does_not_bypass_universe_completeness_or_cache(tmp_path, bad_data):
    def handler(request):
        if request.url.host == '82.push2.eastmoney.com':
            return httpx.Response(302, headers={'Location': delayed(request)})
        return httpx.Response(200, json=dict(rc=0, data=bad_data))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.CashCowSourceError):
            m.EastmoneyCashCowSource(client, cache_dir=tmp_path).universe()
    assert not list(tmp_path.glob('*.json'))


def test_non_stream_client_is_also_constrained():
    calls = []
    class Client:
        def get(self, url, **kwargs):
            assert kwargs['follow_redirects'] is False
            calls.append(url)
            request = httpx.Request('GET', url)
            if len(calls) == 1:
                return httpx.Response(302, headers={'Location': delayed(request)}, request=request)
            return httpx.Response(200, json={}, request=request)
    assert m.EastmoneyCashCowSource(Client())._get(m.UNIVERSE, {}) == {}
    assert len(calls) == 2
