"""Bounded readers for public TDX downloads; never a TDX protocol client.

Archive filenames, together with the requested URL, identify the daily date.
The first four md1 bytes are not a date. Corporate event maximum dates do NOT
prove full coverage through that date, nor the absence of adjustment factors.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import math
import os
from pathlib import Path
import re
import struct
import tempfile
import zipfile

from app.services.market_sina import SinaDataError, SinaRateLimitError, _day

DAILY_BASE_URL = 'https://www.tdx.com.cn/products/data/data/g4day'
CORPORATE_URL = 'https://www.tdx.com.cn/products/data/data/dbf/gbbq.zip'
MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 96 * 1024 * 1024


def _invalid(message):
    return SinaDataError(message, code='invalid_tdx_archive')


def _members(data):
    if not data or len(data) > MAX_ZIP_BYTES:
        raise _invalid('TDX archive exceeds size bounds or is empty.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if (not infos or len(infos) > 32 or len(set(names)) != len(names)
                    or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES
                    or any(i.flag_bits & 1 or i.is_dir() or '/' in i.filename
                           or '\\' in i.filename or '..' in i.filename for i in infos)):
                raise _invalid('Unsafe or oversized TDX archive members.')
            return {i.filename: archive.read(i) for i in infos}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise _invalid('Invalid TDX ZIP payload.') from exc


def _download(client, url):
    # Streaming bounds both error responses and successful downloads. One request,
    # no retry and no alternate endpoint after access denial.
    with client.stream('GET', url) as response:
        if response.status_code in (403, 429):
            raise SinaRateLimitError(response.status_code,
                                     retry_after=response.headers.get('retry-after'))
        response.raise_for_status()
        if str(response.url) != url:
            raise _invalid('TDX redirected to a different source URL.')
        data = bytearray()
        for block in response.iter_bytes():
            data.extend(block)
            if len(data) > MAX_ZIP_BYTES:
                raise _invalid('TDX response exceeds size bounds.')
        return bytes(data), dict(response.headers)


def _atomic_cache(root, filename, data):
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.tdx-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, root / filename)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _numeric(value):
    if not math.isfinite(value) or value < 0:
        raise _invalid('Invalid TDX price, volume, or amount.')
    return format(Decimal(str(value)), 'f')


class TdxDailyArchives:
    def __init__(self, client, cache_root=None, base_url=DAILY_BASE_URL):
        self.client = client
        self.cache_root = Path(cache_root) if cache_root is not None else None
        self.base_url = base_url.rstrip('/')

    def _cache_identity(self, day):
        day = _day(day)
        stamp = day.strftime('%Y%m%d')
        url = f'{self.base_url}/{stamp}.zip'
        # Cache identity includes the source URL, not only the date.
        filename = f'{stamp}-{hashlib.sha256(url.encode()).hexdigest()[:16]}.zip'
        return day, url, filename

    def invalidate(self, day):
        """Quarantine rejected cached bytes without fetching or retrying anything.

        The caller decides whether another attempt is allowed. In particular,
        access-denied/rate-limit responses must never trigger automatic retries.
        Archive integrity alone cannot establish source publication completeness.
        """
        _day_value, _url, filename = self._cache_identity(day)
        if self.cache_root is None:
            return False
        path = self.cache_root / filename
        if not path.exists():
            return False
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        # Atomic rename preserves the exact rejected payload for audit. Identical
        # bytes share the same audit name; this path is never a fetch cache hit.
        audit = self.cache_root / f'{filename[:-4]}.rejected-{digest.hexdigest()}.zip'
        os.replace(path, audit)
        return True

    def fetch(self, day):
        day, url, filename = self._cache_identity(day)
        path = self.cache_root / filename if self.cache_root else None
        cached = path is not None and path.exists()
        if cached:
            if path.stat().st_size > MAX_ZIP_BYTES:
                raise _invalid('Cached TDX payload exceeds size bounds.')
            data = path.read_bytes()
        else:
            data, _headers = _download(self.client, url)
        rows = self.parse(data, day, source_url=url)
        if not cached:
            _atomic_cache(self.cache_root, filename, data)
        return dict(date=day.isoformat(), verified=True, publication_complete=None,
                    rows=list(rows.values()),
                    source_digest=hashlib.sha256(data).hexdigest(), source_url=url)

    @staticmethod
    def parse(data, day, *, source_url=''):
        day = _day(day)
        stamp = day.strftime('%y%m%d')
        members = _members(data)
        digest = hashlib.sha256(data).hexdigest()
        rows = {}
        for market in ('sh', 'sz'):
            cn, mn = f'{market}{stamp}.cod', f'{market}{stamp}.md1'
            if cn not in members or mn not in members:
                raise _invalid('TDX archive does not match requested date/markets.')
            codes, values = members[cn], members[mn]
            count = len(codes) // 150
            if (not count or len(codes) % 150 or len(values) % 512
                    or count != len(values) // 512):
                raise _invalid('TDX daily record lengths are inconsistent.')
            seen = set()
            for offset in range(0, len(codes), 150):
                rec = codes[offset:offset + 150]
                try:
                    code = rec[:6].decode('ascii')
                    # TDX truncates some long security names mid-GBK character.
                    # Preserve the quote and signal lossy display text, not prices.
                    name_bytes = rec[40:72].split(b'\0', 1)[0]
                    name = name_bytes.decode('gbk', errors='replace').strip()
                except UnicodeError as exc:
                    raise _invalid('Invalid TDX symbol/name encoding.') from exc
                symbol = market + code
                seq = struct.unpack_from('<H', rec, 32)[0]
                if (not re.fullmatch(r'[0-9]{6}', code) or symbol in rows
                        or seq in seen or seq >= count):
                    raise _invalid('TDX duplicate or invalid symbol/sequence.')
                seen.add(seq)
                block = values[seq * 512:(seq + 1) * 512]
                prev, opening, high, low, close = struct.unpack_from('<5d', block, 4)
                volume = struct.unpack_from('<Q', block, 56)[0]
                amount = struct.unpack_from('<d', block, 72)[0]
                for number in (prev, opening, high, low, close, amount):
                    _numeric(number)
                # Whole archives include aggregate statistics with turnover but
                # no price. Keep them without pretending they are valid quotes.
                # Universe consumers must require quote_valid for active rows.
                quote_valid = close > 0 or (volume == 0 and amount == 0)
                rows[symbol] = dict(date=day.isoformat(), symbol=symbol, name=name,
                                    name_encoding_valid='�' not in name, quote_valid=quote_valid,
                                    close=_numeric(close), raw_close=_numeric(close), prev_close=_numeric(prev),
                                    volume=str(volume), amount=_numeric(amount),
                                    source_url=source_url, source_digest=digest)
        return rows


class TdxCorporateActions:
    """Validate gbbq with an explicitly supplied public lookup table (1042 words, optionally two padding words).

    No remote source is executed. The table is deliberately not vendored until
    its redistribution license is established. An absent table fails closed.
    Neither a valid package nor its HTTP Last-Modified proves full event coverage.
    """
    def __init__(self, client, cache_root=None, base_url=CORPORATE_URL, *, lookup_table=None):
        self.client = client
        self.cache_root = Path(cache_root) if cache_root is not None else None
        self.base_url = base_url
        self.lookup_table = lookup_table

    def _decode(self, block):
        k = self.lookup_table
        a, b = struct.unpack('<II', block)
        a ^= k[17]
        for j in range(16, 0, -1):
            f = ((k[18 + (a >> 24)] + k[274 + ((a >> 16) & 255)]) & 0xffffffff)
            f ^= k[530 + ((a >> 8) & 255)]
            f = ((f + k[786 + (a & 255)]) & 0xffffffff) ^ k[j]
            a, b = b ^ f, a
        return struct.pack('<II', b ^ k[0], a)

    def fetch(self):
        k = self.lookup_table
        if (not isinstance(k, (list, tuple)) or len(k) not in (1042, 1044)
                or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in k)):
            raise SinaDataError('A verified public gbbq lookup table is required.',
                                code='tdx_decoder_unavailable')
        # This mutable URL must be re-fetched, never considered fresh from cache.
        data, headers = _download(self.client, self.base_url)
        members = _members(data)
        if 'gbbq' not in members:
            raise _invalid('Unexpected gbbq archive members.')
        payload = members['gbbq']
        if len(payload) < 4:
            raise _invalid('Truncated gbbq header.')
        count = struct.unpack_from('<I', payload)[0]
        if not count or len(payload) != 4 + count * 29:
            raise _invalid('Truncated or inconsistent gbbq record count.')
        rows = []
        for i in range(count):
            raw = payload[4 + i * 29:4 + (i + 1) * 29]
            clear = b''.join(self._decode(raw[j:j + 8]) for j in (0, 8, 16)) + raw[24:]
            market, code, stamp, category, c1, c2, c3, c4 = struct.unpack('<B7sIBffff', clear)
            try:
                code = code.rstrip(b'\0').decode('ascii')
                day = datetime.strptime(str(stamp), '%Y%m%d').date()
            except (UnicodeError, ValueError) as exc:
                raise _invalid('Invalid gbbq date/symbol.') from exc
            if (not re.fullmatch(r'[0-9]{6}', code)
                    or not 1900 <= day.year <= 2100 or category == 0
                    or not all(math.isfinite(v) for v in (c1, c2, c3, c4))):
                raise _invalid('Invalid gbbq record.')
            prefix = {0: 'sz', 1: 'sh', 2: 'bj'}.get(market)
            rows.append(dict(symbol=prefix + code if prefix else None, market=market, code=code,
                             date=day.isoformat(), category=category,
                             values=[format(Decimal(str(v)), 'f') for v in (c1, c2, c3, c4)]))
        digest = hashlib.sha256(data).hexdigest()
        result = dict(records=rows, actions=[r for r in rows if r['category'] == 1],
                      source_url=self.base_url, source_digest=digest,
                      retrieved_at=datetime.now(timezone.utc).isoformat(),
                      last_modified=headers.get('last-modified'),
                      latest_event_date=max(r['date'] for r in rows),
                      coverage_verified=False, complete_through=None,
                      unknown_markets=sorted({r['market'] for r in rows if r['symbol'] is None}),
                      unknown_categories=sorted({r['category'] for r in rows if r['category'] != 1}),
                      coverage_reason='Archive integrity is verified, event completeness and freshness are not.')
        _atomic_cache(self.cache_root, f'gbbq-{digest}.zip', data)
        return result
