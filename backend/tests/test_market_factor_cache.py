from datetime import date, datetime
from hashlib import sha256
import json
import os

import pytest

from app.services.market_factor_cache import SinaFactorCache, MAX_BYTES
from app.services.market_public import parse_factors
from app.services.market_sina import SinaDataError

DAY = date(2026, 11, 12)
SYMBOL = 'sh600000'
TEXT = 'var sh600000qfq={"total":1,"data":[{"d":"2020-01-01","f":"1.234567890123456789"}]};'


def cache(tmp_path):
    return SinaFactorCache(tmp_path, parse_factors)


def test_same_capture_day_reuses_original_precision_across_instances(tmp_path):
    cache(tmp_path).save(SYMBOL, DAY, TEXT)
    result = cache(tmp_path).load(SYMBOL, DAY)
    assert result == TEXT
    assert str(parse_factors(result, SYMBOL)[0][1]) == '1.234567890123456789'


def test_next_capture_day_requires_refresh_not_quote_date(tmp_path):
    c = cache(tmp_path)
    c.save(SYMBOL, DAY, TEXT)
    assert c.load(SYMBOL, date(2026, 11, 13)) is None
    assert c.load(SYMBOL, DAY) == TEXT


def test_new_capture_overwrites_without_accumulating_old_files(tmp_path):
    c = cache(tmp_path)
    c.save(SYMBOL, DAY, TEXT)
    c.save(SYMBOL, date(2026, 11, 13), TEXT)
    assert len(list(c.root.iterdir())) == 1
    assert c.load(SYMBOL, date(2026, 11, 13)) == TEXT
    with pytest.raises(SinaDataError):
        c.load(SYMBOL, DAY)


def test_missing_and_disabled_cache(tmp_path):
    assert cache(tmp_path).load(SYMBOL, DAY) is None
    for c in (SinaFactorCache(None, parse_factors),
              SinaFactorCache(tmp_path, parse_factors, source_url='https://example.com/{symbol}')):
        c.save(SYMBOL, DAY, TEXT)
        assert c.load(SYMBOL, DAY) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('text', ['', 'invalid', 'var sh600000qfq={"total":0,"data":[]};',
                                  TEXT.replace('sh600000', 'sh600001'), 'x' * (MAX_BYTES + 1)])
def test_invalid_empty_and_wrong_identity_never_saved(tmp_path, text):
    c = cache(tmp_path)
    with pytest.raises(SinaDataError):
        c.save(SYMBOL, DAY, text)
    assert c.load(SYMBOL, DAY) is None


@pytest.mark.parametrize('field,value', [('symbol', 'sh600001'), ('source_id', 'other'),
                                        ('source_url', 'https://example.com'),
                                        ('captured_day', '2026-11-13'), ('text', 'invalid')])
def test_tampering_rejected_even_with_recomputed_digest(tmp_path, field, value):
    c = cache(tmp_path)
    c.save(SYMBOL, DAY, TEXT)
    path = c.root / (SYMBOL + '.json')
    data = json.loads(path.read_text())
    data['record'][field] = value
    raw = json.dumps(data['record'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    data['sha256'] = sha256(raw).hexdigest()
    path.write_text(json.dumps(data))
    with pytest.raises(SinaDataError):
        c.load(SYMBOL, DAY)


def test_digest_mismatch(tmp_path):
    c = cache(tmp_path)
    c.save(SYMBOL, DAY, TEXT)
    path = c.root / (SYMBOL + '.json')
    path.write_text(path.read_text().replace('1.234567890', '2.234567890'))
    with pytest.raises(SinaDataError):
        c.load(SYMBOL, DAY)


@pytest.mark.parametrize('content', [b'{', b'\xff', b'x' * (MAX_BYTES + 1)])
def test_bad_file_rejected(tmp_path, content):
    c = cache(tmp_path)
    c.root.mkdir()
    (c.root / (SYMBOL + '.json')).write_bytes(content)
    with pytest.raises(SinaDataError):
        c.load(SYMBOL, DAY)


def test_symlink_directory_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    c = cache(tmp_path)
    c.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SinaDataError):
        c.save(SYMBOL, DAY, TEXT)
    with pytest.raises(SinaDataError):
        c.load(SYMBOL, DAY)
    assert not list(outside.iterdir())


def test_symlink_file_rejected_and_save_does_not_follow_it(tmp_path):
    c = cache(tmp_path)
    c.root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    (c.root / (SYMBOL + '.json')).symlink_to(outside)
    with pytest.raises(SinaDataError):
        c.load(SYMBOL, DAY)
    c.save(SYMBOL, DAY, TEXT)
    assert outside.read_text() == 'untouched'
    assert not (c.root / (SYMBOL + '.json')).is_symlink()
    assert os.stat(c.root / (SYMBOL + '.json')).st_mode & 0o777 == 0o600


@pytest.mark.parametrize('symbol,day', [('../escape', DAY), ('sh600000/a', DAY), (SYMBOL, '2026-11-12'),
                                      (SYMBOL, datetime(2026, 11, 12))])
def test_invalid_identity(tmp_path, symbol, day):
    with pytest.raises(SinaDataError):
        cache(tmp_path).load(symbol, day)


def test_fifo_rejected_without_blocking(tmp_path):
    c = cache(tmp_path)
    c.root.mkdir()
    os.mkfifo(c.root / (SYMBOL + '.json'))
    with pytest.raises(SinaDataError):
        c.load(SYMBOL, DAY)


def test_parser_must_return_nonempty_proof(tmp_path):
    c = SinaFactorCache(tmp_path, lambda text, symbol: [])
    with pytest.raises(SinaDataError):
        c.save(SYMBOL, DAY, TEXT)
    assert not list(tmp_path.iterdir())


def test_atomic_failure_preserves_previous_and_cleans_temp(tmp_path, monkeypatch):
    c = cache(tmp_path)
    c.save(SYMBOL, DAY, TEXT)
    def fail(*args, **kwargs):
        raise OSError('disk failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(SinaDataError):
        c.save(SYMBOL, DAY, TEXT.replace('1.23456789', '2.23456789'))
    assert c.load(SYMBOL, DAY) == TEXT
    assert [p.name for p in c.root.iterdir()] == [SYMBOL + '.json']



def test_invalidate_removes_own_entry_and_allows_refresh(tmp_path):
    c = cache(tmp_path)
    c.save(SYMBOL, DAY, TEXT)
    assert c.invalidate(SYMBOL, DAY) is True
    assert c.load(SYMBOL, DAY) is None
    assert c.invalidate(SYMBOL, DAY) is False
    c.save(SYMBOL, DAY, TEXT)
    assert c.load(SYMBOL, DAY) == TEXT


def test_invalidate_missing_and_disabled(tmp_path):
    assert cache(tmp_path).invalidate(SYMBOL, DAY) is False
    assert SinaFactorCache(None, parse_factors).invalidate(SYMBOL, DAY) is False
    assert not list(tmp_path.iterdir())


def test_invalidate_symlink_does_not_follow_target(tmp_path):
    c = cache(tmp_path)
    c.root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    link = c.root / (SYMBOL + '.json')
    link.symlink_to(outside)
    assert c.invalidate(SYMBOL, DAY) is True
    assert outside.read_text() == 'untouched'
    assert not link.is_symlink()


def test_invalidate_rejects_symlink_directory(tmp_path):
    c = cache(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / (SYMBOL + '.json')
    target.write_text('untouched')
    c.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SinaDataError):
        c.invalidate(SYMBOL, DAY)
    assert target.read_text() == 'untouched'
