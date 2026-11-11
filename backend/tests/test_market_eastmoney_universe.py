"""Synthetic offline contracts only. No captured production/source fixtures."""
from copy import deepcopy
import json
import logging

import httpx
import pytest

from app.services import market_eastmoney_universe as em
from app.services.market_universe import UniverseDataError

DAY = '2026-11-09'


def row(code='600001', market='SH', **changes):
    result = {'SECURITY_CODE': code, 'SECUCODE': code + '.' + market,
              'SECURITY_NAME_ABBR': '测试', 'TRADE_MARKET': '069001001001',
              'TRADE_DATE': DAY + ' 00:00:00', 'CLOSE_PRICE': 10.0, 'CHANGE_RATE': 0}
    result.update(changes)
    return result


def payload(rows=None, count=None, pages=1):
    rows = [row(), row('302132', 'SZ'), row('689009'), row('920001', 'BJ')] if rows is None else rows
    return {'success': True, 'code': 0, 'result': {'count': len(rows) if count is None else count,
                                                'pages': pages, 'data': rows}}


def adapter(responses, **kwargs):
    requests = []
    def respond(request):
        requests.append(request)
        item = responses[len(requests) - 1]
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)
    return em.EastmoneyDatedUniverse(httpx.Client(transport=httpx.MockTransport(respond)), **kwargs), requests


def test_identity_and_no_false_claims(caplog):
    source, requests = adapter([payload()])
    with caplog.at_level(logging.INFO, logger=em.__name__):
        result = source.fetch(DAY)
    assert {r['symbol'] for r in result['rows']} == {'sh600001', 'sh689009', 'sz302132'}
    assert result['expected_count'] == 3 and result['evidence']['report_count'] == 4
    assert result['evidence']['report_pages_complete']
    assert not result['evidence']['membership_verified']
    assert not result['evidence']['listing_dates_verified']
    assert all('listed_from' not in r and 'delisted_on' not in r and r['date'] == DAY for r in result['rows'])
    assert result['rows'][1]['evidence']['name_basis'] == 'latest_vendor_backfill_not_historical_st'
    assert requests[0].url.host == 'datacenter-web.eastmoney.com'
    assert dict(requests[0].url.params) == {k: str(v) for k, v in source._params(DAY, 1).items()}
    assert all(word in caplog.text for word in ('start', 'cache', 'page', 'coverage', 'elapsed='))
    assert '测试' not in caplog.text and 'https://' not in caplog.text


def test_complete_two_pages_no_prefix_filter():
    rows = [row(f'{i:06d}', 'SH') for i in range(em.PAGE_SIZE)] + [row('302132', 'SZ')]
    source, requests = adapter([payload(rows[:5000], 5001, 2), payload(rows[5000:], 5001, 2)])
    result = source.fetch(DAY)
    assert result['expected_count'] == 5001
    assert result['rows'][0]['symbol'] == 'sh000000'
    assert [r.url.params['pageNumber'] for r in requests] == ['1', '2']


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(success=False), lambda p: p.update(code=9201),
    lambda p: p.update(code=False), lambda p: p.update(result=None),
    lambda p: p['result'].update(count=5), lambda p: p['result'].update(pages=2),
    lambda p: p['result'].update(count='4'), lambda p: p['result'].update(count=0),
    lambda p: p['result']['data'].__setitem__(1, deepcopy(p['result']['data'][0])),
    lambda p: p['result']['data'][0].update(TRADE_DATE='2026-11-10 00:00:00'),
    lambda p: p['result']['data'][0].update(SECUCODE='600002.SH'),
    lambda p: p['result']['data'][0].update(SECUCODE='600001.HK'),
    lambda p: p['result']['data'][0].pop('CLOSE_PRICE'),
    lambda p: p['result']['data'][0].pop('CHANGE_RATE'),
    lambda p: p['result']['data'][0].update(CLOSE_PRICE='10'),
    lambda p: p['result']['data'][0].update(CLOSE_PRICE=True),
    lambda p: p['result']['data'][0].update(CLOSE_PRICE=-1),
    lambda p: p['result']['data'][0].update(CHANGE_RATE='NaN'),
    lambda p: p['result']['data'][0].update(SECURITY_NAME_ABBR=''),
    lambda p: p['result']['data'][0].update(TRADE_MARKET=None),
])
def test_invalid_reports_not_cached(tmp_path, mutation):
    data = payload()
    mutation(data)
    source, requests = adapter([data], cache_root=tmp_path)
    with pytest.raises(UniverseDataError):
        source.fetch(DAY)
    assert len(requests) == 1 and not list(tmp_path.iterdir())


def test_nullable_fields_unknown_boards():
    data = payload()
    data['result']['data'][0].update(CLOSE_PRICE=None, CHANGE_RATE=None, TRADE_MARKET='future-board')
    source, _ = adapter([data])
    assert source.fetch(DAY)['rows'][0]['board'] == 'future-board'


@pytest.mark.parametrize('status', [403, 429, 302, 500])
def test_http_stops(status, tmp_path):
    source, requests = adapter([httpx.Response(status)], cache_root=tmp_path)
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(DAY)
    assert caught.value.status_code == status
    assert caught.value.is_rate_limited == (status in (403, 429))
    assert len(requests) == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('body', [b'not JSON', b'null', b'{"success":true,"code":0,"result":{"count":2,"pages":1,"data":[]}}'])
def test_bad_json_schema(body):
    source, _ = adapter([httpx.Response(200, content=body)])
    with pytest.raises(UniverseDataError):
        source.fetch(DAY)


@pytest.mark.parametrize('change', ['count', 'pages', 'missing', 'duplicate', 'date'])
def test_bad_second_page_no_partial_cache(tmp_path, change):
    rows = [row(f'{i:06d}', 'SH') for i in range(5000)]
    first, second = payload(rows, 5001, 2), payload([row('302132', 'SZ')], 5001, 2)
    if change == 'count':
        second['result']['count'] = 5002
    elif change == 'pages':
        second['result']['pages'] = 3
    elif change == 'missing':
        second['result']['data'] = []
    elif change == 'duplicate':
        second['result']['data'] = [rows[0]]
    else:
        second['result']['data'][0]['TRADE_DATE'] = '2026-11-08'
    source, requests = adapter([first, second], cache_root=tmp_path)
    with pytest.raises(UniverseDataError):
        source.fetch(DAY)
    assert len(requests) == 2 and not list(tmp_path.iterdir())


def test_cache_reuse(tmp_path):
    source, requests = adapter([payload()], cache_root=tmp_path)
    first = source.fetch(DAY)
    source2, requests2 = adapter([], cache_root=tmp_path)
    second = source2.fetch(DAY)
    assert second['evidence']['cache_hit']
    assert first['evidence']['report_digest'] == second['evidence']['report_digest']
    assert len(requests) == 1 and not requests2
    assert source._cache_path(DAY) != source._cache_path('2026-11-10')


@pytest.mark.parametrize('mode', ['truncated', 'digest', 'date', 'schema'])
def test_cache_corruption_no_same_run_refetch(tmp_path, mode):
    source, requests = adapter([payload(), payload()], cache_root=tmp_path)
    source.fetch(DAY)
    path = source._cache_path(DAY)
    content = json.loads(path.read_bytes())
    if mode == 'truncated':
        path.write_bytes(b'{')
    else:
        if mode == 'digest':
            content['sha256'] = '0' * 64
        elif mode == 'date':
            content['date'] = '2026-11-10'
        else:
            content['pages'][0]['result']['data'][0]['TRADE_DATE'] = '2026-11-10'
            content['sha256'] = em.sha256(em._encoded(content['pages'])).hexdigest()
        path.write_bytes(em._encoded(content))
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(DAY)
    assert caught.value.code == 'cache_corrupt'
    assert not path.exists() and len(requests) == 1
    assert not source.fetch(DAY)['evidence']['cache_hit']


def test_atomic_failure(tmp_path, monkeypatch):
    source, _ = adapter([payload()], cache_root=tmp_path)
    def fail(*args, **kwargs):
        raise OSError('simulated')
    monkeypatch.setattr(em.os, 'replace', fail)
    with pytest.raises(UniverseDataError):
        source.fetch(DAY)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('limit', ['entries', 'bytes'])
def test_cache_bounds_preserve_unrelated(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(em, 'MAX_CACHE_ENTRIES' if limit == 'entries' else 'MAX_CACHE_TOTAL_BYTES',
                        1 if limit == 'entries' else 2000)
    unrelated = tmp_path / 'user.json'
    unrelated.write_text('preserve')
    source, _ = adapter([payload()], cache_root=tmp_path)
    old = source._cache_path('2026-11-08')
    old.write_bytes(b'x' * 1900)
    source.fetch(DAY)
    assert not old.exists() and unrelated.read_text() == 'preserve'
    assert source._cache_path(DAY).exists()


def test_deadline_stops_before_request():
    ticks = iter([0, 0, 700])
    source, requests = adapter([payload()], monotonic=lambda: next(ticks))
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(DAY)
    assert caught.value.code == 'catalogue_deadline' and not requests


def test_nonfinite_rejected():
    source, _ = adapter([httpx.Response(200, content=json.dumps(payload()).replace('10.0', 'NaN'))])
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(DAY)
    assert caught.value.code == 'invalid_numeric'


def test_stream_size_bound(monkeypatch):
    from app.services import market_universe
    monkeypatch.setattr(market_universe, '_MAX_WORKBOOK_BYTES', 10)
    source, _ = adapter([payload()])
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(DAY)
    assert caught.value.code == 'response_too_large'


@pytest.mark.parametrize('kind', ['root', 'ancestor', 'file', 'dangling_file'])
def test_symlink_boundaries(tmp_path, kind):
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'target.json'
    target.write_text('preserve')
    cache = tmp_path / 'cache'
    cache.mkdir()
    if kind in ('root', 'ancestor'):
        cache.rmdir()
        cache.symlink_to(outside, target_is_directory=True)
        if kind == 'ancestor':
            cache = cache / 'child'
    source, requests = adapter([payload()], cache_root=cache)
    if kind in ('file', 'dangling_file'):
        source._cache_path(DAY).symlink_to(target if kind == 'file' else outside / 'missing')
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(DAY)
    assert caught.value.code == 'cache_corrupt' and not requests
    assert target.read_text() == 'preserve' and not (outside / 'child').exists()
    if kind in ('file', 'dangling_file'):
        assert source._cache_path(DAY).is_symlink()


def test_permission_denial_preserves_cache(tmp_path, monkeypatch):
    source, requests = adapter([payload()], cache_root=tmp_path)
    source.fetch(DAY)
    real_open = em.os.open
    def denied(name, flags, *args, **kwargs):
        if name == source._cache_path(DAY).name:
            raise PermissionError('denied')
        return real_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(em.os, 'open', denied)
    with pytest.raises(UniverseDataError):
        source.fetch(DAY)
    assert len(requests) == 1 and source._cache_path(DAY).exists()


class Cancelled(ValueError):
    stop_collection = True


def test_cache_validation_cancel_preserves_file(tmp_path):
    source, requests = adapter([payload()], cache_root=tmp_path)
    source.fetch(DAY)
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 9:
            raise Cancelled('cancelled')
    source.client.check = check
    with pytest.raises(Cancelled):
        source.fetch(DAY)
    assert calls == 9 and source._cache_path(DAY).exists() and len(requests) == 1


def test_write_cancel_cleans_temporary(tmp_path):
    source, _ = adapter([payload()], cache_root=tmp_path)
    def check():
        if list(tmp_path.glob(f'.{em.SOURCE_ID}-*')):
            raise Cancelled('cancelled')
    source.client.check = check
    with pytest.raises(Cancelled):
        source.fetch(DAY)
    assert not list(tmp_path.iterdir())


def test_safe_directory_contract(tmp_path):
    with em.safe_cache_directory(None) as directory:
        assert directory is None
    root = tmp_path / 'new' / 'cache'
    with pytest.raises(FileNotFoundError):
        with em.safe_cache_directory(root):
            pytest.fail('missing path')
    with em.safe_cache_directory(root, create=True) as directory:
        assert em.stat.S_ISDIR(em.os.fstat(directory).st_mode)
    with pytest.raises(OSError):
        em.os.fstat(directory)
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        with em.safe_cache_directory(link):
            pytest.fail('symlink')
    error = Cancelled('original')
    with pytest.raises(Cancelled) as caught:
        with em.safe_cache_directory(tmp_path):
            raise error
    assert caught.value is error
