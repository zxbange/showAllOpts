"""Offline candidate contracts only; no successful live endpoint probe claimed."""
from hashlib import sha256
import json
import os
import zlib

import httpx
import pytest

from app.services import market_eastmoney_listing as em
from app.services.market_universe import UniverseDataError

DAY = '2026-11-11'
SYMBOL = 'sh688827'


def payload(**fields):
    return {'rc': 0, 'data': {'f57': '688827', 'f13': 1, 'f58': '测试', 'f189': 20261110, **fields}}


def adapter(responses, **kwargs):
    requests = []
    def respond(request):
        requests.append(request)
        item = responses[len(requests) - 1]
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return em.EastmoneyListingMetadata(client, **kwargs), requests


def test_contract_identity_request_digest_no_interval_claim():
    raw = json.dumps(payload()).encode()
    source, requests = adapter([httpx.Response(200, content=raw)])
    assert source.fetch(SYMBOL, DAY) == {
        'symbol': SYMBOL, 'exchange': 'SSE', 'ipo_date': '2026-11-10',
        'source_id': em.SOURCE_ID, 'evidence_version': 1, 'request_secid': '1.688827',
        'verified': True, 'source_digest': sha256(raw).hexdigest(),
        'source_schema': em.SCHEMA, 'raw_response': raw.decode()}
    assert str(requests[0].url).startswith(em.URL + '?')
    assert dict(requests[0].url.params) == {
        'secid': '1.688827', 'fields': 'f57,f58,f189,f13', 'fltt': '2', 'invt': '2'}


@pytest.mark.parametrize('symbol,fields,expected', [
    ('sz302132', {'f57': '302132', 'f13': 0, 'f189': '20100827'}, '2010-08-27'),
    ('sh689009', {'f57': '689009', 'f189': 20201029}, '2020-10-29'),
])
def test_ipo_not_code_change(symbol, fields, expected):
    source, requests = adapter([payload(**fields)])
    result = source.fetch(symbol, DAY)
    assert result['ipo_date'] == expected
    assert result['exchange'] == ('SZSE' if symbol.startswith('sz') else 'SSE')
    assert requests[0].url.params['secid'] == ('0.' if symbol.startswith('sz') else '1.') + symbol[2:]
    assert 'listed_from' not in result and 'historical_st_verified' not in result


@pytest.mark.parametrize('fields', [
    {'f57': '688828'}, {'f57': 688827}, {'f57': '0688827'},
    {'f13': 0}, {'f13': '1'}, {'f13': True}, {'f13': 1.0},
    {'f58': ''}, {'f58': ' '}, {'f58': '-'}, {'f58': None},
    {'f189': 0}, {'f189': True}, {'f189': False}, {'f189': None},
    {'f189': '00000000'}, {'f189': '20260230'}, {'f189': 20261301},
    {'f189': '2026-11-10'}, {'f189': ' 20261110'}, {'f189': 20261110.0},
    {'f189': 20261112}, {'f189': '２０２６１１１０'},
])
def test_bad_fields_no_cache_or_fallback(tmp_path, fields):
    source, requests = adapter([payload(**fields)], cache_root=tmp_path)
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'dated_membership_gap'
    assert caught.value.source_diagnostics['source_id'] == em.SOURCE_ID
    assert len(requests) == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('field', ['f57', 'f13', 'f58', 'f189'])
def test_missing_fields(field):
    data = payload()
    del data['data'][field]
    source, requests = adapter([data])
    with pytest.raises(UniverseDataError):
        source.fetch(SYMBOL, DAY)
    assert len(requests) == 1


@pytest.mark.parametrize('data', [None, [], {}, {'rc': False, 'data': {}},
    {'rc': '0', 'data': {}}, {'rc': 0.0, 'data': {}}, {'rc': 1, 'data': {}},
    {'rc': 0, 'data': None}, {'rc': 0, 'data': []}])
def test_api_errors(data):
    source, requests = adapter([data])
    with pytest.raises(UniverseDataError):
        source.fetch(SYMBOL, DAY)
    assert len(requests) == 1


@pytest.mark.parametrize('status', [403, 429, 302, 500])
def test_http_failure_no_body_or_retry(status):
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail('Failure body must not be read')
            yield b''
    source, requests = adapter([httpx.Response(status, stream=Unreadable())])
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.status_code == status
    assert caught.value.is_rate_limited == (status in (403, 429))
    assert caught.value.source_diagnostics['source_id'] == em.SOURCE_ID
    assert len(requests) == 1


def test_cache_reuse_across_days_and_future_for_target_kept(tmp_path):
    source, requests = adapter([payload()], cache_root=tmp_path)
    first = source.fetch(SYMBOL, DAY)
    path = source.cache_root / source._cache_name(SYMBOL)
    before, content = path.stat(), path.read_bytes()
    second, requests2 = adapter([], cache_root=tmp_path)
    assert second.fetch(SYMBOL, '2026-11-10') == first
    with pytest.raises(UniverseDataError):
        second.fetch(SYMBOL, '2026-11-09')
    assert second.fetch(SYMBOL, '2026-11-12') == first
    assert path.read_bytes() == content and path.stat().st_mtime_ns == before.st_mtime_ns
    assert len(requests) == 1 and requests2 == []


@pytest.mark.parametrize('mode', ['truncated', 'digest', 'symbol', 'source', 'version', 'url', 'schema', 'identity', 'oversize'])
def test_corrupt_cache_removed_no_same_attempt_retry(tmp_path, mode):
    source, requests = adapter([payload(), payload()], cache_root=tmp_path)
    source.fetch(SYMBOL, DAY)
    path = source.cache_root / source._cache_name(SYMBOL)
    envelope = json.loads(path.read_bytes())
    if mode == 'truncated':
        raw = b'{'
    elif mode == 'oversize':
        raw = b' ' * (em.MAX_BYTES + 1)
    else:
        key = {'digest': 'sha256', 'source': 'source_id'}.get(mode, mode)
        if mode == 'identity':
            envelope['raw'] = json.dumps(payload(f13=0))
            envelope['sha256'] = sha256(envelope['raw'].encode()).hexdigest()
        else:
            envelope[key] = True if mode == 'version' else 'wrong'
        raw = json.dumps(envelope).encode()
    path.write_bytes(raw)
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'cache_corrupt'
    assert len(requests) == 1 and not path.exists()
    assert source.fetch(SYMBOL, DAY)['ipo_date'] == '2026-11-10'
    assert len(requests) == 2 and path.exists()


@pytest.mark.parametrize('kind', ['file', 'directory', 'ancestor', 'fifo'])
def test_unsafe_cache_paths_fail_closed(tmp_path, kind):
    actual = tmp_path / 'actual'
    actual.mkdir()
    root = tmp_path / 'cache'
    if kind == 'ancestor':
        root.symlink_to(actual, target_is_directory=True)
    else:
        root.mkdir()
        dedicated = root / 'eastmoney-listing-v1'
        if kind == 'directory':
            dedicated.symlink_to(actual, target_is_directory=True)
        else:
            dedicated.mkdir()
            target = actual / 'payload'
            target.write_text('untouched')
            path = dedicated / f'{em.SOURCE_ID}-v1-{SYMBOL}.json'
            if kind == 'fifo':
                os.mkfifo(path)
            else:
                path.symlink_to(target)
    source, requests = adapter([], cache_root=root)
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'cache_corrupt' and requests == []
    if kind in ('file', 'fifo'):
        assert target.read_text() == 'untouched'


def test_atomic_failure_no_temporary_or_published_file(tmp_path, monkeypatch):
    source, requests = adapter([payload()], cache_root=tmp_path)
    def fail(*args, **kwargs):
        raise OSError('publication failure')
    monkeypatch.setattr(em.os, 'link', fail)
    with pytest.raises(UniverseDataError):
        source.fetch(SYMBOL, DAY)
    assert len(requests) == 1 and list(source.cache_root.iterdir()) == []


def test_cancel_propagates_unchanged(tmp_path):
    class Cancelled(ValueError):
        stop_collection = True
    source, requests = adapter([payload()], cache_root=tmp_path)
    source.fetch(SYMBOL, DAY)
    error = Cancelled('stop')
    def check():
        raise error
    source.client.check = check
    with pytest.raises(Cancelled) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value is error and len(requests) == 1


def test_permission_denial_not_suppressed(tmp_path, monkeypatch):
    source, requests = adapter([], cache_root=tmp_path)
    error = PermissionError('denied')
    def denied(*args, **kwargs):
        raise error
    monkeypatch.setattr(em.os, 'open', denied)
    with pytest.raises(PermissionError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value is error and requests == []


@pytest.mark.parametrize('compressed', [False, True])
def test_response_bounds(compressed):
    raw = b' ' * (em.MAX_BYTES + 1)
    if compressed:
        raw = zlib.compress(raw)
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield raw
    source, requests = adapter([httpx.Response(200, stream=Stream(),
        headers={'content-encoding': 'deflate'} if compressed else {})])
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'response_too_large' and len(requests) == 1


def test_bounded_cache_enumeration_preserves_files(tmp_path, monkeypatch):
    source, requests = adapter([payload()], cache_root=tmp_path)
    source.cache_root.mkdir()
    (source.cache_root / 'existing').write_text('preserve')
    monkeypatch.setattr(em, 'MAX_CACHE_ENUMERATION', 0)
    with pytest.raises(UniverseDataError):
        source.fetch(SYMBOL, DAY)
    assert len(requests) == 1
    assert (source.cache_root / 'existing').read_text() == 'preserve'
    assert len(list(source.cache_root.iterdir())) == 1


@pytest.mark.parametrize('symbol', ['SH688827', '688827', 'sh68882', 'bj920001', 'sh../a'])
def test_invalid_requested_symbol_never_requests(symbol):
    source, requests = adapter([])
    with pytest.raises(UniverseDataError):
        source.fetch(symbol, DAY)
    assert requests == []


def test_request_count_budget(monkeypatch):
    source, requests = adapter([payload()])
    monkeypatch.setattr(em, 'MAX_REQUESTS', 1)
    source.fetch(SYMBOL, DAY)
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'catalogue_deadline' and len(requests) == 1


def test_active_budget_excludes_between_calls(monkeypatch):
    source, requests = adapter([payload(), payload()])
    clock = [0.0]
    source._monotonic = lambda: clock[0]
    original = source._validate
    def validate(*args):
        result = original(*args)
        clock[0] += 2
        return result
    source._validate = validate
    monkeypatch.setattr(em, 'MAX_ACTIVE_SECONDS', 3.0)
    source.fetch(SYMBOL, DAY)
    clock[0] += 5000
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'catalogue_deadline' and len(requests) == 2


def test_transport_failure_recognizable():
    def fail(request):
        raise httpx.ConnectTimeout('timeout')
    source = em.EastmoneyListingMetadata(httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'network_error'
    assert caught.value.source_diagnostics['transport_category'] == 'connect_timeout'


def test_oldest_eviction_keeps_saved_proof_and_other_files(tmp_path, monkeypatch):
    source, requests = adapter([payload(), payload(f57='689009', f189=20201029)], cache_root=tmp_path)
    first = source.fetch(SYMBOL, DAY)
    original = json.dumps(first, sort_keys=True)
    old = source.cache_root / source._cache_name(SYMBOL)
    other = source.cache_root / 'unrelated.json'
    other.write_text('untouched')
    link = source.cache_root / 'unrelated-link'
    link.symlink_to(other)
    monkeypatch.setattr(em, 'MAX_CACHE_ENTRIES', 1)
    second = source.fetch('sh689009', DAY)
    assert not old.exists()
    assert em.validate_listing_evidence(first, SYMBOL, DAY) == first
    assert json.dumps(first, sort_keys=True) == original
    assert em.validate_listing_evidence(second, 'sh689009', DAY) == second
    assert other.read_text() == 'untouched' and link.is_symlink() and len(requests) == 2


def test_byte_eviction(tmp_path, monkeypatch):
    source, _ = adapter([payload(), payload(f57='689009', f189=20201029)], cache_root=tmp_path)
    source.fetch(SYMBOL, DAY)
    old = source.cache_root / source._cache_name(SYMBOL)
    monkeypatch.setattr(em, 'MAX_CACHE_TOTAL_BYTES', old.stat().st_size + 32)
    source.fetch('sh689009', DAY)
    assert not old.exists()


@pytest.mark.parametrize('key,value', [
    ('symbol', 'sz688827'), ('exchange', 'SZSE'), ('ipo_date', '2026-11-09'),
    ('source_id', 'exchange_master'), ('evidence_version', True),
    ('evidence_version', 2), ('request_secid', '0.688827'), ('verified', 1),
    ('source_digest', '0' * 64), ('source_schema', 'other'), ('raw_response', '{}'),
    ('raw_response', ' ' * (em.MAX_BYTES + 1)),
])
def test_saved_proof_tampering(key, value):
    source, _ = adapter([payload()])
    proof = source.fetch(SYMBOL, DAY)
    proof[key] = value
    with pytest.raises(UniverseDataError):
        em.validate_listing_evidence(proof, SYMBOL, DAY)


def test_saved_proof_future_target():
    source, _ = adapter([payload()])
    proof = source.fetch(SYMBOL, DAY)
    with pytest.raises(UniverseDataError):
        em.validate_listing_evidence(proof, SYMBOL, '2026-11-09')


def test_own_symlink_never_deleted_on_eviction(tmp_path, monkeypatch):
    source, _ = adapter([payload()], cache_root=tmp_path)
    source.cache_root.mkdir()
    target = tmp_path / 'protected'
    target.write_text('untouched')
    link = source.cache_root / source._cache_name('sh689009')
    link.symlink_to(target)
    monkeypatch.setattr(em, 'MAX_CACHE_ENTRIES', 1)
    with pytest.raises(UniverseDataError):
        source.fetch(SYMBOL, DAY)
    assert link.is_symlink() and target.read_text() == 'untouched'


def test_inode_replacement_not_unlinked(tmp_path):
    source, _ = adapter([])
    path = tmp_path / 'entry'
    path.write_text('old')
    info = path.stat()
    replacement = tmp_path / 'replacement'
    replacement.write_text('new')
    replacement.replace(path)
    with em.safe_cache_directory(tmp_path) as directory:
        with pytest.raises(UniverseDataError):
            source._unlink_same(directory, path.name, info)
    assert path.read_text() == 'new'


@pytest.mark.parametrize('phase', ['before_open', 'before_corrupt_unlink'])
def test_known_cache_disappearance_no_http(tmp_path, monkeypatch, phase):
    source, requests = adapter([payload()], cache_root=tmp_path)
    source.fetch(SYMBOL, DAY)
    path = source.cache_root / source._cache_name(SYMBOL)
    if phase == 'before_open':
        original_open = em.os.open
        def racing_open(name, *args, **kwargs):
            if name == path.name:
                path.unlink()
            return original_open(name, *args, **kwargs)
        monkeypatch.setattr(em.os, 'open', racing_open)
    else:
        path.write_text('{')
        original_unlink = source._unlink_same
        def racing_unlink(directory, name, expected):
            path.unlink()
            return original_unlink(directory, name, expected)
        monkeypatch.setattr(source, '_unlink_same', racing_unlink)
    with pytest.raises(UniverseDataError) as caught:
        source.fetch(SYMBOL, DAY)
    assert caught.value.code == 'cache_corrupt'
    assert len(requests) == 1 and not path.exists()


@pytest.mark.parametrize('proof', [None, [], {}, {'raw_response': None}, {'raw_response': 1},
    {'raw_response': '{}'}, {'raw_response': 'null'}, {'raw_response': '[]'},
    {'raw_response': '\ud800'}])
def test_malformed_proofs(proof):
    with pytest.raises(UniverseDataError):
        em.validate_listing_evidence(proof, SYMBOL, DAY)


def test_current_symbol_never_overwritten(tmp_path):
    source, _ = adapter([payload()], cache_root=tmp_path)
    source.fetch(SYMBOL, DAY)
    path = source.cache_root / source._cache_name(SYMBOL)
    before = path.read_bytes()
    with pytest.raises(UniverseDataError):
        source._save(SYMBOL, json.dumps(payload(f189=20201029)).encode())
    assert path.read_bytes() == before


def test_transient_connection_retried_once_and_timeout_is_sixty_seconds(caplog):
    calls = []
    def serve(request):
        calls.append(request)
        assert request.extensions['timeout']['connect'] == 60.0
        assert request.extensions['timeout']['read'] == 60.0
        if len(calls) == 1:
            raise httpx.ConnectTimeout('secret must not be logged')
        return httpx.Response(200, json=payload())
    source = em.EastmoneyListingMetadata(httpx.Client(transport=httpx.MockTransport(serve)))
    assert source.fetch(SYMBOL, DAY)['ipo_date'] == '2026-11-10'
    assert len(calls) == 2
    assert 'secret must not be logged' not in caplog.text
    assert SYMBOL in caplog.text and DAY in caplog.text
    assert em.REQUEST_SECONDS == 60.0


def test_connection_retry_exhausted_is_bounded():
    calls = []
    def serve(request):
        calls.append(request)
        raise httpx.ConnectTimeout('temporary')
    source = em.EastmoneyListingMetadata(httpx.Client(transport=httpx.MockTransport(serve)))
    with pytest.raises(UniverseDataError):
        source.fetch(SYMBOL, DAY)
    assert len(calls) == 2
