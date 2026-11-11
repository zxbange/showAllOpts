"""Candidate Eastmoney stock/get f189 contract, not a live-probed schema.

AKShare identifies f189 as IPO date. Runtime schema and identity verification
is not independent historical membership, historical ST, or exchange-interval
verification. Code-change dates must never substitute for an IPO date.
"""
from contextlib import contextmanager
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

import httpx

from app.services.market_diagnostics import source_failure_context
from app.services.market_eastmoney_universe import safe_cache_directory
from app.services.market_universe import ExchangeSecurityMaster, UniverseDataError, _day, _ResponseBody

URL = 'https://push2.eastmoney.com/api/qt/stock/get'
SOURCE_ID = 'eastmoney_listing_metadata'
VERSION = 1
SCHEMA = 'eastmoney_stock_get_f189_v1'
MAX_REQUESTS = 32
MAX_ACTIVE_SECONDS = 180.0
REQUEST_SECONDS = 30.0
MAX_BYTES = 64 * 1024
MAX_CACHE_ENUMERATION = 1024
MAX_CACHE_ENTRIES = 128
MAX_CACHE_TOTAL_BYTES = 8 * 1024 * 1024


def _invalid(message, code='dated_membership_gap'):
    return UniverseDataError(message, code=code)


def _identity(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r'(sh|sz)[0-9]{6}', symbol):
        raise _invalid('上市元数据证券标识无效。')
    market = 1 if symbol.startswith('sh') else 0
    return market, f'{market}.{symbol[2:]}'


def _parse_listing_raw(raw, symbol):
    market, secid = _identity(symbol)
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            raise ValueError('oversized or wrong type')
        raw_text = raw.decode('utf-8')
        payload = json.loads(raw_text)
    except (ValueError, RecursionError):
        raise _invalid('上市元数据不是有效 JSON。', 'invalid_json') from None
    if (not isinstance(payload, dict) or type(payload.get('rc')) is not int
            or payload['rc'] != 0 or not isinstance(payload.get('data'), dict)):
        raise _invalid('上市元数据接口错误或结构无效。')
    data = payload['data']
    if (data.get('f57') != symbol[2:] or type(data.get('f13')) is not int
            or data['f13'] != market):
        raise _invalid('上市元数据证券代码或市场不匹配。')
    name, value = data.get('f58'), data.get('f189')
    if not isinstance(name, str) or not name.strip() or name.strip() == '-':
        raise _invalid('上市元数据名称缺失。')
    if type(value) not in (int, str) or not re.fullmatch(r'[0-9]{8}', str(value)):
        raise _invalid('上市元数据 IPO 日期缺失或无效。')
    text = str(value)
    try:
        ipo = date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        raise _invalid('上市元数据 IPO 日期无效。') from None
    return {'symbol': symbol, 'exchange': 'SSE' if market == 1 else 'SZSE',
            'ipo_date': ipo.isoformat(), 'source_id': SOURCE_ID,
            'evidence_version': VERSION, 'request_secid': secid, 'verified': True,
            'source_digest': sha256(raw).hexdigest(), 'source_schema': SCHEMA,
            'raw_response': raw_text}


@source_failure_context(source_id=SOURCE_ID, stage='metadata_validation')
def validate_listing_evidence(proof, symbol, day):
    """Revalidate a self-contained saved IPO proof without network or cache I/O."""
    if not isinstance(proof, dict) or not isinstance(proof.get('raw_response'), str):
        raise _invalid('上市元数据证据缺少原始响应。')
    if len(proof['raw_response']) > MAX_BYTES:
        raise _invalid('上市元数据证据响应过大。')
    try:
        raw = proof['raw_response'].encode('utf-8')
    except UnicodeError:
        raise _invalid('上市元数据证据编码无效。') from None
    expected = _parse_listing_raw(raw, symbol)
    if (type(proof.get('evidence_version')) is not int or proof.get('verified') is not True
            or any(proof.get(key) != value for key, value in expected.items())
            or date.fromisoformat(expected['ipo_date']) > _day(day)):
        raise _invalid('上市元数据证据标识、日期或摘要无效。')
    return expected


class _BoundedRaw(httpx.SyncByteStream):
    def __init__(self, iterator, check):
        self.iterator, self.check = iterator, check

    def __iter__(self):
        total = 0
        while True:
            self.check()
            try:
                block = next(self.iterator)
            except StopIteration:
                self.check()
                return
            self.check()
            total += len(block)
            if total > MAX_BYTES:
                raise _invalid('上市元数据响应过大。', 'response_too_large')
            yield block


class EastmoneyListingMetadata(ExchangeSecurityMaster):
    """Caller owns HTTP pacing/cancellation. No retries, redirects or fallback.

    Immutable successful metadata is cached independently of target date.
    Corrupt own regular entries are removed after inode checks and fail this
    attempt. Oldest own entries may be evicted; saved proofs retain raw bodies.
    """
    def __init__(self, client, *, cache_root=None):
        super().__init__(client, timeout=httpx.Timeout(10.0, read=20.0))
        self.cache_root = Path(cache_root) / 'eastmoney-listing-v1' if cache_root is not None else None
        self._request_count = 0
        self._active_seconds = 0.0
        self._active_request_deadline = None

    _identity = staticmethod(_identity)

    def _check_budget(self, request_deadline=None, status_code=None):
        check = getattr(self.client, 'check', None)
        if callable(check):
            check()
        if self._active_request_deadline is not None:
            request_deadline = (min(request_deadline, self._active_request_deadline)
                                if request_deadline is not None else self._active_request_deadline)
        super()._check_budget(request_deadline, status_code)

    @contextmanager
    def _catalogue_budget(self):
        # Exclude time spent on unrelated per-stock factors between fetches.
        started = self._monotonic()
        self._catalogue_deadline = started + max(0, MAX_ACTIVE_SECONDS - self._active_seconds)
        try:
            self._check_budget()
            yield
            self._check_budget()
        finally:
            self._active_seconds += max(0, self._monotonic() - started)
            self._catalogue_deadline = None

    @source_failure_context(stage='request')
    def _request(self, url, params, requests):
        if self._request_count >= MAX_REQUESTS:
            raise _invalid('上市元数据请求次数达到本次采集上限。', 'catalogue_deadline')
        self._request_count += 1
        self._active_request_deadline = self._monotonic() + REQUEST_SECONDS
        try:
            return super()._request(url, params, requests)
        finally:
            self._active_request_deadline = None

    def _read_body(self, response, request_deadline):
        check = lambda: self._check_budget(request_deadline, response.status_code)
        check()
        decoded = None
        if response.is_stream_consumed:
            chunks = response.iter_bytes()
        else:
            decoded = httpx.Response(response.status_code, headers=response.headers,
                stream=_BoundedRaw(iter(response.iter_raw()), check))
            chunks = decoded.iter_bytes()
        content = bytearray()
        try:
            for chunk in chunks:
                check()
                if len(content) + len(chunk) > MAX_BYTES:
                    raise _invalid('上市元数据响应过大。', 'response_too_large')
                content.extend(chunk)
            check()
        finally:
            if decoded is not None:
                decoded.close()
        return _ResponseBody(response.status_code, bytes(content))

    @source_failure_context(stage='metadata_validation')
    def _validate(self, raw, symbol):
        self._check_budget()
        return _parse_listing_raw(raw, symbol)

    def _cache_name(self, symbol):
        return f'{SOURCE_ID}-v{VERSION}-{symbol}.json'

    @staticmethod
    def _regular(directory, name):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise _invalid('上市元数据缓存不是有效普通文件。', 'cache_corrupt')
        return info

    def _unlink_same(self, directory, name, expected):
        self._check_budget()
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)):
            raise _invalid('上市元数据缓存已被替换，拒绝删除。', 'cache_corrupt')
        os.unlink(name, dir_fd=directory)

    @source_failure_context(stage='cache_read')
    def _load(self, symbol):
        if self.cache_root is None:
            return None
        self._check_budget()
        known_entry = False
        try:
            with safe_cache_directory(self.cache_root) as directory:
                name = self._cache_name(symbol)
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                known_entry = True
                if not stat.S_ISREG(info.st_mode):
                    raise _invalid('上市元数据缓存不是普通文件。', 'cache_corrupt')
                try:
                    if info.st_size > MAX_BYTES:
                        raise ValueError('oversized')
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(fd, 'rb') as handle:
                        opened = os.fstat(handle.fileno())
                        if (not stat.S_ISREG(opened.st_mode)
                                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                            raise _invalid('上市元数据缓存读取期间被替换。', 'cache_corrupt')
                        self._check_budget()
                        content = handle.read(MAX_BYTES + 1)
                        self._check_budget()
                    envelope = json.loads(content)
                    raw = envelope['raw'].encode('utf-8')
                    if (len(content) > MAX_BYTES or envelope['source_id'] != SOURCE_ID
                            or type(envelope['version']) is not int or envelope['version'] != VERSION
                            or envelope['symbol'] != symbol or envelope['url'] != URL
                            or envelope['schema'] != SCHEMA
                            or sha256(raw).hexdigest() != envelope['sha256']):
                        raise ValueError('cache identity/digest')
                    self._validate(raw, symbol)
                except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
                    if (getattr(error, 'stop_collection', False)
                            or getattr(error, 'code', None) in ('request_deadline', 'catalogue_deadline')):
                        raise
                    self._unlink_same(directory, name, info)
                    raise _invalid('上市元数据缓存损坏；已移除原缓存，本次不重新请求。', 'cache_corrupt') from None
                return raw
        except FileNotFoundError:
            if known_entry:
                raise _invalid('上市元数据缓存读取期间消失；本次不重新请求。', 'cache_corrupt') from None
            return None
        except PermissionError:
            raise
        except OSError as error:
            if getattr(error, 'stop_collection', False):
                raise
            raise _invalid('上市元数据缓存不可安全读取。', 'cache_corrupt') from None

    @source_failure_context(stage='cache_write')
    def _save(self, symbol, raw):
        if self.cache_root is None:
            return
        self._check_budget()
        try:
            content = json.dumps({'source_id': SOURCE_ID, 'version': VERSION, 'symbol': symbol,
                'url': URL, 'schema': SCHEMA, 'sha256': sha256(raw).hexdigest(),
                'raw': raw.decode('utf-8')}, ensure_ascii=False).encode('utf-8')
        except UnicodeError:
            raise _invalid('上市元数据编码无效。', 'invalid_json') from None
        if len(content) > MAX_BYTES:
            raise _invalid('上市元数据缓存过大。', 'response_too_large')
        try:
            with safe_cache_directory(self.cache_root, create=True) as directory:
                total, own = 0, []
                with os.scandir(directory) as entries:
                    for count, entry in enumerate(entries, 1):
                        self._check_budget()
                        if count > MAX_CACHE_ENUMERATION:
                            raise _invalid('上市元数据缓存目录条目过多。', 'cache_corrupt')
                        if not re.fullmatch(rf'{SOURCE_ID}-v{VERSION}-(sh|sz)[0-9]{{6}}\.json', entry.name):
                            continue
                        if entry.name == self._cache_name(symbol):
                            raise _invalid('上市元数据缓存已存在，拒绝覆盖。', 'cache_corrupt')
                        info = self._regular(directory, entry.name)
                        own.append((entry.name, info))
                        total += info.st_size
                own.sort(key=lambda pair: (pair[1].st_mtime_ns, pair[0]))
                while own and (len(own) >= MAX_CACHE_ENTRIES or total + len(content) > MAX_CACHE_TOTAL_BYTES):
                    self._check_budget()
                    name, info = own.pop(0)
                    self._unlink_same(directory, name, info)
                    total -= info.st_size
                if MAX_CACHE_ENTRIES < 1 or total + len(content) > MAX_CACHE_TOTAL_BYTES:
                    raise _invalid('上市元数据缓存容量不足。', 'cache_corrupt')
                temporary = f'.{uuid4().hex}.tmp'
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._check_budget()
                    # Atomic no-clobber publication, including concurrent writers.
                    os.link(temporary, self._cache_name(symbol), src_dir_fd=directory,
                            dst_dir_fd=directory, follow_symlinks=False)
                    os.fsync(directory)
                    self._check_budget()
                finally:
                    os.unlink(temporary, dir_fd=directory)
        except PermissionError:
            raise
        except OSError as error:
            if getattr(error, 'stop_collection', False):
                raise
            raise _invalid('上市元数据缓存不可安全写入。', 'cache_corrupt') from None

    @source_failure_context(source_id=SOURCE_ID, stage='metadata_validation')
    def fetch(self, symbol, day):
        target = _day(day)
        _, secid = self._identity(symbol)
        with self._catalogue_budget():
            self._check_budget()
            raw = self._load(symbol)
            cached = raw is not None
            if not cached:
                response = self._request(URL, {'secid': secid, 'fields': 'f57,f58,f189,f13',
                    'fltt': 2, 'invt': 2}, [])
                raw = response.content
            result = self._validate(raw, symbol)
            if date.fromisoformat(result['ipo_date']) > target:
                raise _invalid('IPO 日期晚于目标日期，不能证明历史成员资格。')
            if not cached:
                self._save(symbol, raw)
            return result
