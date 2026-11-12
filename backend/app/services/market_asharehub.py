"""Single-session AShareHub raw observations, never a security universe or MA20.

Only unadjusted close, volume (lots -> shares), and amount (thousands -> yuan)
are consumed. The public-market orchestrator still proves dated membership and
pauses, and computes the original 20-market-session adjusted average itself.
"""
from decimal import Decimal, localcontext
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

from app.services.market_diagnostics import (
    record_http_failure, record_os_failure, record_source_progress, source_failure_context,
)
from app.services.market_eastmoney_universe import safe_cache_directory
from app.services.market_sina import SinaDataError, SinaRateLimitError, _day, _number

URL = 'https://asharehub.com/v2/market/daily'
SOURCE_ID = 'asharehub_daily'
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20000
DOWNLOAD_BUDGET = 120.0
TIMEOUT = httpx.Timeout(45.0, connect=8.0)
LOG = logging.getLogger(__name__)


def _invalid():
    return SinaDataError('AShareHub 单日行情结构或日期未通过校验。', code='snapshot_invalid')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode()


def _reject_constant(value):
    raise _invalid()


def _scale(value, factor):
    # Numeric input validation permits more digits than Decimal's default
    # context. Unit conversion must be exact or fail, never silently round.
    with localcontext() as context:
        context.prec = 80
        return _number(_number(value) * Decimal(factor))


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class AShareHubDailyArchives:
    """One fixed HTTPS endpoint, no redirects, retries or fallback after denial.

    Persist only compact validated raw observations, not headers, credentials,
    provider error messages or derived factors. Cache identity is source/date
    specific. Publication completeness is established by the independent audit.
    """
    source_id = SOURCE_ID

    def __init__(self, client, api_key, cache_root=None):
        self.client = client
        self._api_key = api_key
        self.cache_root = Path(cache_root) / 'asharehub-raw-daily-v2' if cache_root is not None else None

    def _check(self):
        check = getattr(self.client, 'check', None)
        if callable(check):
            check()

    @staticmethod
    def _url(day):
        return URL + '?trade_date=' + day.strftime('%Y%m%d')

    @staticmethod
    def _filename(day):
        return str(day) + '.json'

    @staticmethod
    def parse(body, day):
        day = _day(day)
        try:
            if not body or len(body) > MAX_RESPONSE_BYTES:
                raise _invalid()
            payload = json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)
            if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_ROWS:
                raise _invalid()
            rows, seen = [], set()
            stamp = day.strftime('%Y%m%d')
            for item in payload:
                if not isinstance(item, dict) or item.get('trade_date') != stamp:
                    raise _invalid()
                match = re.fullmatch(r'(\d{6})\.(SH|SZ|BJ)', str(item.get('symbol', '')))
                if match is None or item['symbol'] in seen:
                    raise _invalid()
                seen.add(item['symbol'])
                close = _number(item['close'], positive=True)
                volume = _scale(item['vol'], '100')
                amount = _scale(item['amount'], '1000')
                # Missing/zero-volume rows cannot affirm a pause. The caller
                # requires independent full-session evidence even for zero rows.
                if volume != volume.to_integral_value() or (volume == 0) != (amount == 0):
                    raise _invalid()
                rows.append(dict(symbol=match[2].lower() + match[1], date=str(day),
                                 close=_text(close), volume=_text(volume),
                                 amount=_text(amount)))
            digest = sha256(body).hexdigest()
            return dict(date=str(day), verified=True, publication_complete=None,
                        source_id=SOURCE_ID, source_digest=digest,
                        source_url=AShareHubDailyArchives._url(day), rows=rows)
        except (KeyError, ValueError, TypeError, ArithmeticError, RecursionError):
            raise _invalid() from None

    def _load(self, day):
        if self.cache_root is None:
            return None
        try:
            with safe_cache_directory(self.cache_root) as directory:
                fd = os.open(self._filename(day), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
                with os.fdopen(fd, 'rb') as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise _invalid()
                    body = handle.read(MAX_CACHE_BYTES + 1)
            self._check()
            if len(body) > MAX_CACHE_BYTES:
                raise _invalid()
            payload = json.loads(body)
            result = payload['result']
            if (payload['version'] != 1 or sha256(_encoded(result)).hexdigest() != payload['sha256']
                    or result['date'] != str(day) or result['source_id'] != SOURCE_ID
                    or result['source_url'] != self._url(day) or result['verified'] is not True
                    or result['publication_complete'] is not None
                    or re.fullmatch(r'[0-9a-f]{64}', result['source_digest']) is None):
                raise _invalid()
            # Revalidate compact data; the stored digest is integrity metadata,
            # not permission to skip numeric/date/duplicate validation.
            if not isinstance(result['rows'], list) or not 1 <= len(result['rows']) <= MAX_ROWS:
                raise _invalid()
            wire_rows = []
            for row in result['rows']:
                if row['date'] != str(day) or re.fullmatch(r'(sh|sz|bj)\d{6}', row['symbol']) is None:
                    raise _invalid()
                wire_rows.append(dict(symbol=row['symbol'][2:] + '.' + row['symbol'][:2].upper(),
                    trade_date=day.strftime('%Y%m%d'), close=row['close'],
                    vol=str(_scale(row['volume'], '0.01')), amount=str(_scale(row['amount'], '0.001'))))
            validated = self.parse(_encoded(wire_rows), day)
            if validated['rows'] != result['rows']:
                # Decimal strings may retain harmless trailing zeroes; compare
                # their numeric meaning, then return normalized validated rows.
                for original, checked in zip(result['rows'], validated['rows']):
                    if any(_number(original[key]) != _number(checked[key]) for key in ('close', 'volume', 'amount')):
                        raise _invalid()
            return validated | {'source_digest': result['source_digest']}
        except FileNotFoundError:
            return None
        except (KeyError, ValueError, TypeError, ArithmeticError, RecursionError):
            raise _invalid() from None

    def _save(self, day, result):
        if self.cache_root is None:
            return
        body = _encoded(dict(version=1, sha256=sha256(_encoded(result)).hexdigest(), result=result))
        if len(body) > MAX_CACHE_BYTES:
            raise _invalid()
        with safe_cache_directory(self.cache_root, create=True) as directory:
            name = '.daily-' + uuid4().hex
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check()
                os.replace(name, self._filename(day), src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def invalidate(self, day):
        """Quarantine rejected compact observations; never retry in this call."""
        if self.cache_root is None:
            return False
        try:
            with safe_cache_directory(self.cache_root) as directory:
                name = self._filename(_day(day))
                # Atomic rename does not follow a symlink or read its target.
                os.rename(name, name + '.rejected-' + uuid4().hex,
                          src_dir_fd=directory, dst_dir_fd=directory)
            return True
        except FileNotFoundError:
            return False

    def fetch(self, day):
        started = time.monotonic()
        body = bytearray()
        source_date = None
        cache_state = 'disabled' if self.cache_root is None else 'miss'
        try:
            with source_failure_context(source_id=SOURCE_ID):
                day = _day(day)
                source_date = str(day)
                self._check()
                with source_failure_context(stage='cache_read'):
                    cached = self._load(day)
                if cached is not None:
                    return cached
                with source_failure_context(stage='request'):
                    key = self._api_key.get_secret_value().strip()
                    if not key or len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
                        raise SinaRateLimitError(403)
                    url = self._url(day)
                    with self.client.stream('GET', url, headers={'X-API-Key': key, 'Accept': 'application/json'},
                                            timeout=TIMEOUT, follow_redirects=False) as response:
                        del key
                        if response.status_code in (401, 403, 429):
                            raise SinaRateLimitError(403 if response.status_code == 401 else response.status_code)
                        response.raise_for_status()
                        if str(response.url) != url or response.status_code != 200:
                            raise _invalid()
                        for block in response.iter_bytes():
                            self._check()
                            body.extend(block)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise _invalid()
                            if time.monotonic() - started >= DOWNLOAD_BUDGET:
                                raise SinaDataError('AShareHub 下载超过时间限制。', code='request_deadline')
                with source_failure_context(stage='records_parse'):
                    result = self.parse(body, day)
                with source_failure_context(stage='cache_write'):
                    self._save(day, result)
                LOG.info('Market source daily complete source_id=%s source_date=%s records=%d elapsed_ms=%d',
                         SOURCE_ID, source_date, len(result['rows']), int((time.monotonic() - started) * 1000))
                return result
        except Exception as error:
            if not getattr(error, 'stop_collection', False):
                record_http_failure(error, error)
                record_os_failure(error, error)
                record_source_progress(error, source_date=source_date, cache_state=cache_state,
                                       archive_bytes=len(body), elapsed_ms=int((time.monotonic() - started) * 1000))
            raise
