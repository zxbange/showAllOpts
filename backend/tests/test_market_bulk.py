import io
import struct
import zipfile

import httpx
import pytest

from app.services.market_bulk import TdxDailyArchives, TdxCorporateActions
from app.services.market_sina import SinaDataError, SinaRateLimitError


def zipped(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buf.getvalue()


def daily(*, seq=0, close=10., code=b'689009', date='261106', volume=100):
    members = {}
    for market in ('sh', 'sz'):
        cod, md = bytearray(150), bytearray(512)
        cod[:6] = code
        struct.pack_into('<H', cod, 32, seq)
        cod[40:44] = '测试'.encode('gbk')
        struct.pack_into('<I', md, 0, 123456789)  # explicitly not a date
        struct.pack_into('<5d', md, 4, 9., 10., 11., 8., close)
        struct.pack_into('<Q', md, 56, volume)
        struct.pack_into('<d', md, 72, 1000. if volume else 0.)
        members[market + date + '.cod'] = cod
        members[market + date + '.md1'] = md
    return zipped(members)


def client(payload, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, content=payload)))


def test_daily_cdr_and_cache(tmp_path):
    calls = []
    def get(request):
        calls.append(request.url)
        return httpx.Response(200, content=daily())
    source = TdxDailyArchives(httpx.Client(transport=httpx.MockTransport(get)), tmp_path)
    result = source.fetch('2026-11-06')
    assert result['verified'] is True
    assert result['rows'][0]['symbol'] == 'sh689009'
    assert result['rows'][0]['raw_close'] == '10.0'
    assert result['rows'][0]['volume'] == '100'
    assert len(result['source_digest']) == 64
    assert source.fetch('2026-11-06') == result
    assert len(calls) == 1


@pytest.mark.parametrize('kwargs', [dict(seq=1), dict(close=float('nan')),
                                    dict(code=b'68a009'), dict(date='261105')])
def test_invalid_daily_never_cached(tmp_path, kwargs):
    with pytest.raises(SinaDataError):
        TdxDailyArchives(client(daily(**kwargs)), tmp_path).fetch('2026-11-06')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('status', [403, 429])
def test_rate_limit_stops(tmp_path, status):
    with pytest.raises(SinaRateLimitError) as error:
        TdxDailyArchives(client(b'denied', status), tmp_path).fetch('2026-11-06')
    assert error.value.status_code == status
    assert not list(tmp_path.iterdir())


def test_wrong_lengths_and_duplicate_sequences():
    with zipfile.ZipFile(io.BytesIO(daily())) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    members['sh261106.cod'] += members['sh261106.cod']
    with pytest.raises(SinaDataError):
        TdxDailyArchives.parse(zipped(members), '2026-11-06')
    members['sh261106.md1'] *= 2
    with pytest.raises(SinaDataError):
        TdxDailyArchives.parse(zipped(members), '2026-11-06')


def test_zero_inactive_close_allowed():
    assert TdxDailyArchives.parse(daily(close=0, volume=0), '2026-11-06')['sh689009']['raw_close'] == '0.0'


def test_corporate_missing_decoder_fails_before_request():
    with pytest.raises(SinaDataError, match='lookup table'):
        TdxCorporateActions(None).fetch()


def test_corporate_invalid_length_not_cached(tmp_path):
    payload = zipped({'gbbq': struct.pack('<I', 1) + b'x' * 28})
    with pytest.raises(SinaDataError):
        TdxCorporateActions(client(payload), tmp_path, lookup_table=[0] * 1042).fetch()
    assert not list(tmp_path.iterdir())


def test_corporate_validated_records_do_not_assert_coverage(tmp_path, monkeypatch):
    raw = struct.pack('<B7sIBffff', 1, b'689009', 20261106, 1, 1., 0., 0., 0.)
    payload = zipped({'gbbq': struct.pack('<I', 1) + raw})
    source = TdxCorporateActions(client(payload), tmp_path, lookup_table=[0] * 1042)
    monkeypatch.setattr(source, '_decode', lambda block: block)
    result = source.fetch()
    assert result['actions'][0]['symbol'] == 'sh689009'
    assert result['coverage_verified'] is False
    assert result['complete_through'] is None
    assert result['latest_event_date'] == '2026-11-06'
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('member', ['../gbbq', '/gbbq', 'directory/gbbq'])
def test_unsafe_names_rejected(member):
    with pytest.raises(SinaDataError):
        TdxDailyArchives.parse(zipped({member: b'x'}), '2026-11-06')


def test_positive_turnover_without_close_is_not_a_valid_quote():
    row = TdxDailyArchives.parse(daily(close=0), '2026-11-06')['sh689009']
    assert row['quote_valid'] is False


def test_corporate_invalid_historical_sentinel_fails_closed(tmp_path, monkeypatch):
    raw = struct.pack('<B7sIBffff', 1, b'200106', 18991230, 1, 0., 0., 0., 0.)
    source = TdxCorporateActions(client(zipped({'gbbq': struct.pack('<I', 1) + raw})),
                                 tmp_path, lookup_table=[0] * 1044)
    monkeypatch.setattr(source, '_decode', lambda b: b)
    with pytest.raises(SinaDataError):
        source.fetch()
    assert not list(tmp_path.iterdir())


def test_corporate_unknown_category_and_market_are_not_discarded(monkeypatch):
    raw = struct.pack('<B7sIBffff', 99, b'200106', 20261106, 255, 0., 0., 0., 0.)
    source = TdxCorporateActions(client(zipped({'gbbq': struct.pack('<I', 1) + raw,
                                               'gbbq.map': b'auxiliary'})),
                                 lookup_table=[0] * 1042)
    monkeypatch.setattr(source, '_decode', lambda b: b)
    result = source.fetch()
    assert result['unknown_markets'] == [99]
    assert result['unknown_categories'] == [255]
    assert len(result['records']) == 1
    assert result['coverage_verified'] is False


def test_invalidation_preserves_audit_and_refetches(tmp_path):
    payloads = [daily(close=10), daily(close=11)]
    calls = []
    def get(request):
        calls.append(request.url)
        return httpx.Response(200, content=payloads[len(calls) - 1])
    source = TdxDailyArchives(httpx.Client(transport=httpx.MockTransport(get)), tmp_path)
    first = source.fetch('2026-11-06')
    assert first['publication_complete'] is None
    assert source.invalidate('2026-11-06') is True
    assert len(calls) == 1  # invalidation has no network side effects
    audits = list(tmp_path.glob('*.rejected-*.zip'))
    assert len(audits) == 1
    assert first['source_digest'] in audits[0].name
    assert audits[0].read_bytes() == payloads[0]
    assert source.invalidate('2026-11-06') is False
    second = source.fetch('2026-11-06')
    assert second['source_digest'] != first['source_digest']
    assert second['rows'][0]['close'] == '11.0'
    assert source.fetch('2026-11-06') == second
    assert len(calls) == 2


@pytest.mark.parametrize('status', [403, 429])
def test_invalidated_cache_does_not_retry_denial(tmp_path, status):
    calls = []
    def get(request):
        calls.append(request.url)
        return httpx.Response(200, content=daily()) if len(calls) == 1 else httpx.Response(status)
    source = TdxDailyArchives(httpx.Client(transport=httpx.MockTransport(get)), tmp_path)
    source.fetch('2026-11-06')
    source.invalidate('2026-11-06')
    with pytest.raises(SinaRateLimitError):
        source.fetch('2026-11-06')
    assert len(calls) == 2
    assert len(list(tmp_path.iterdir())) == 1  # preserved audit only


def test_invalidate_without_cache_is_noop():
    assert TdxDailyArchives(None).invalidate('2026-11-06') is False
