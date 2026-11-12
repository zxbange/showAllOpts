"""AShareHub reference directory supplies IPO evidence, not historical membership.

A daily cache is shared across symbols. Missing records are not evidence of IPO,
retirement, suspension or non-membership; callers must retain independent audits.
"""
from datetime import date, datetime, timezone, timedelta
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

import httpx

from app.services.market_diagnostics import source_failure_context
from app.services.market_eastmoney_universe import safe_cache_directory
from app.services.market_sina import SinaDataError, SinaRateLimitError, _day

URL = 'https://asharehub.com/v2/reference/stocks'
SOURCE_ID = 'asharehub_listing_metadata'
VERSION = 1
SCHEMA = 'asharehub_reference_stocks_v1'
REQUEST_SECONDS = 60.0
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20000
LOG = logging.getLogger(__name__)


def _invalid():
    return SinaDataError('AShareHub 上市日期未通过校验。', code='dated_membership_gap')


def _identity(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r'(sh|sz)[0-9]{6}', symbol):
        raise _invalid()
    return symbol[2:] + '.' + symbol[:2].upper()


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{8}', value):
        raise _invalid()
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise _invalid() from None


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _row(item, status):
    if not isinstance(item, dict):
        raise _invalid()
    # The retired catalogue includes namespaced legacy identities such as
    # T600018.SH. Retain that exact identity; never alias it to 600018.SH.
    pattern = r'(T?\d{6})\.(SH|SZ|BJ)' if status == 'D' else r'(\d{6})\.(SH|SZ|BJ)'
    match = re.fullmatch(pattern, str(item.get('symbol', '')))
    if (not match or item.get('list_status') != status
            or item.get('exchange') != {'SH': 'SSE', 'SZ': 'SZSE', 'BJ': 'BSE'}[match[2]]
            or not isinstance(item.get('name'), str) or not item['name'].strip()):
        raise _invalid()
    listed = _date(item.get('list_date'))
    if item.get('delist_date') not in (None, ''):
        if _date(item['delist_date']) < listed:
            raise _invalid()
    elif status == 'D':
        raise _invalid()
    return item


def _proof(raw, symbol):
    wire = _identity(symbol)
    try:
        if not isinstance(raw, bytes) or len(raw) > 16384:
            raise _invalid()
        item = json.loads(raw)
        status = item.get('list_status') if isinstance(item, dict) else None
        if status not in ('L', 'D'):
            raise _invalid()
        _row(item, status)
        if item['symbol'] != wire:
            raise _invalid()
        return dict(symbol=symbol, exchange=item['exchange'], ipo_date=_date(item['list_date']).isoformat(),
            source_id=SOURCE_ID, evidence_version=VERSION, request_secid=wire, verified=True,
            source_digest=sha256(raw).hexdigest(), source_schema=SCHEMA, raw_response=raw.decode())
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise _invalid() from None


def validate_listing_evidence(proof, symbol, day):
    if not isinstance(proof, dict) or not isinstance(proof.get('raw_response'), str):
        raise _invalid()
    expected = _proof(proof['raw_response'].encode(), symbol)
    if (type(proof.get('evidence_version')) is not int or proof.get('verified') is not True
            or any(proof.get(k) != v for k, v in expected.items())
            or date.fromisoformat(expected['ipo_date']) > _day(day)):
        raise _invalid()
    return expected


class AShareHubListingMetadata:
    def __init__(self, client, api_key, *, cache_root=None, now=None, monotonic=None):
        self.client, self._api_key = client, api_key
        self.cache_root = Path(cache_root) / 'asharehub-listing-v1' if cache_root is not None else None
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._catalogues = {}

    def _stamp(self):
        return self._now().astimezone(timezone(timedelta(hours=8))).date().isoformat()

    def _check(self, deadline=None):
        check = getattr(self.client, 'check', None)
        if callable(check):
            check()
        if deadline is not None and self._monotonic() >= deadline:
            raise SinaDataError('上市目录请求超过一分钟。', code='request_deadline')

    @staticmethod
    def parse(body, status):
        try:
            if not body or len(body) > MAX_BYTES or status not in ('L', 'D'):
                raise _invalid()
            rows = json.loads(body)
            if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
                raise _invalid()
            result = {}
            for item in rows:
                _row(item, status)
                if item['symbol'] in result:
                    raise _invalid()
                result[item['symbol']] = item
            return result
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise _invalid() from None

    def _load(self, status, stamp):
        if self.cache_root is None:
            return None
        try:
            with safe_cache_directory(self.cache_root) as directory:
                fd = os.open(status + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise _invalid()
                    content = handle.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise _invalid()
            envelope = json.loads(content)
            if (envelope['source_id'] != SOURCE_ID or envelope['version'] != VERSION
                    or envelope['status'] != status or envelope['url'] != URL):
                raise _invalid()
            if envelope['captured_date'] != stamp:
                return None
            raw = envelope['raw'].encode()
            if sha256(raw).hexdigest() != envelope['sha256']:
                raise _invalid()
            return self.parse(raw, status)
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise _invalid() from None

    def _save(self, status, stamp, raw):
        if self.cache_root is None:
            return
        content = _encode(dict(source_id=SOURCE_ID, version=VERSION, status=status, url=URL,
                               captured_date=stamp, sha256=sha256(raw).hexdigest(), raw=raw.decode()))
        if len(content) > MAX_BYTES:
            raise _invalid()
        with safe_cache_directory(self.cache_root, create=True) as directory:
            temporary = '.' + uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check()
                os.replace(temporary, status + '.json', src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    @source_failure_context(source_id=SOURCE_ID, stage='request')
    def _request(self, status, symbol, target):
        url = URL + '?list_status=' + status
        # Cooperative elapsed budget; an in-flight socket operation is bounded
        # separately by its 60-second phase timeout, not a hard wall-clock timer.
        for attempt in (1, 2):
            deadline = self._monotonic() + REQUEST_SECONDS
            try:
                self._check(deadline)
                key = self._api_key.get_secret_value().strip()
                if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                    raise SinaRateLimitError(403)
                LOG.info('Market listing request source_id=%s symbol=%s date=%s status=%s attempt=%d',
                         SOURCE_ID, symbol, target, status, attempt)
                body = bytearray()
                with self.client.stream('GET', url, headers={'X-API-Key': key, 'Accept': 'application/json'},
                                        timeout=httpx.Timeout(REQUEST_SECONDS), follow_redirects=False) as response:
                    del key
                    self._check(deadline)
                    if response.status_code in (401, 403, 429):
                        raise SinaRateLimitError(403 if response.status_code == 401 else response.status_code)
                    response.raise_for_status()
                    if response.status_code != 200 or str(response.url) != url:
                        raise _invalid()
                    for block in response.iter_bytes():
                        self._check(deadline)
                        body.extend(block)
                        if len(body) > MAX_BYTES:
                            raise _invalid()
                    self._check(deadline)
                return bytes(body)
            except (httpx.ConnectTimeout, httpx.ConnectError) as error:
                LOG.warning('Market listing connection failed source_id=%s symbol=%s date=%s attempt=%d type=%s',
                            SOURCE_ID, symbol, target, attempt, type(error).__name__)
                if attempt == 2:
                    raise
                self._check()

    def _catalogue(self, status, symbol, target):
        stamp = self._stamp()
        key = (status, stamp)
        if key not in self._catalogues:
            self._check()
            rows = self._load(status, stamp)
            if rows is None:
                raw = self._request(status, symbol, target)
                rows = self.parse(raw, status)
                if self._stamp() != stamp:
                    raise _invalid()
                self._save(status, stamp, raw)
            self._catalogues = {k: v for k, v in self._catalogues.items() if k[1] == stamp}
            self._catalogues[key] = rows
        return self._catalogues[key]

    @source_failure_context(source_id=SOURCE_ID, stage='metadata_validation')
    def fetch(self, symbol, day):
        target = _day(day)
        wire = _identity(symbol)
        for status in ('L', 'D'):
            rows = self._catalogue(status, symbol, target)
            if wire in rows:
                proof = _proof(_encode(rows[wire]), symbol)
                return validate_listing_evidence(proof, symbol, target)
        raise SinaDataError('AShareHub 目录没有该证券上市记录。', code='listing_not_found')
