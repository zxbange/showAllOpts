"""Daily-capture cache for the unchanged, full-precision Sina factor parser.

Callers pass the capture date in Asia/Shanghai, not the quote's historical date.
Only today's captured factors may be reused; stale data never masks a failed
refresh. One file per security bounds growth across successive capture dates.
"""
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from app.services.market_eastmoney_universe import safe_cache_directory
from app.services.market_sina import SinaDataError

DEFAULT_FACTOR_URL = 'https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js'
SOURCE_ID = 'sina_qfq_full_precision'
MAX_BYTES = 2 * 1024 * 1024


def _invalid():
    return SinaDataError('复权因子缓存未通过完整性校验。', code='adjustment_unavailable')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class SinaFactorCache:
    """Persist validated raw text, never credentials or rounded numeric factors.

    ``validator(text, symbol)`` must be the original strict factor parser and
    return a non-empty sequence. Custom endpoints deliberately disable caching.
    Invalid cache hits fail closed rather than hiding corrupt evidence.
    """
    def __init__(self, cache_root, validator, *, source_url=DEFAULT_FACTOR_URL):
        self.root = (Path(cache_root) / 'sina-qfq-factors-v1'
                     if cache_root is not None and source_url == DEFAULT_FACTOR_URL else None)
        self.validator = validator

    @staticmethod
    def _identity(symbol, captured_day):
        if not isinstance(symbol, str) or re.fullmatch(r'(sh|sz|bj)[0-9]{6}', symbol) is None:
            raise _invalid()
        if type(captured_day) is not date:
            raise _invalid()
        return symbol + '.json', captured_day.isoformat()

    def _validate_text(self, text, symbol):
        if not isinstance(text, str) or not text or len(text.encode()) > MAX_BYTES:
            raise _invalid()
        if not self.validator(text, symbol):
            raise _invalid()

    def load(self, symbol, captured_day):
        name, capture = self._identity(symbol, captured_day)
        if self.root is None:
            return None
        try:
            with safe_cache_directory(self.root) as directory:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise _invalid()
                    body = handle.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise _invalid()
            payload = json.loads(body)
            record = payload['record']
            if (payload['version'] != 1 or type(payload['version']) is not int
                    or sha256(_encoded(record)).hexdigest() != payload['sha256']
                    or record['source_id'] != SOURCE_ID or record['symbol'] != symbol
                    or record['source_url'] != DEFAULT_FACTOR_URL.format(symbol=symbol)):
                raise _invalid()
            stored_day = date.fromisoformat(record['captured_day'])
            if stored_day.isoformat() != record['captured_day'] or stored_day > captured_day:
                raise _invalid()
            self._validate_text(record['text'], symbol)
            return record['text'] if record['captured_day'] == capture else None
        except FileNotFoundError:
            return None
        except (OSError, KeyError, ValueError, TypeError, UnicodeError, RecursionError):
            raise _invalid() from None

    def invalidate(self, symbol, captured_day):
        """Remove only this security's cache entry without following symlinks.

        Used when a parsed cache hit cannot prove the requested price window.
        Missing/disabled entries are harmless; permission or directory failures
        remain explicit rather than pretending a refresh was made possible.
        """
        name, _ = self._identity(symbol, captured_day)
        if self.root is None:
            return False
        try:
            with safe_cache_directory(self.root) as directory:
                os.unlink(name, dir_fd=directory)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise _invalid() from None

    def save(self, symbol, captured_day, text):
        name, capture = self._identity(symbol, captured_day)
        if self.root is None:
            return
        self._validate_text(text, symbol)
        record = dict(source_id=SOURCE_ID, source_url=DEFAULT_FACTOR_URL.format(symbol=symbol),
                      symbol=symbol, captured_day=capture, text=text)
        body = _encoded(dict(version=1, record=record, sha256=sha256(_encoded(record)).hexdigest()))
        if len(body) > MAX_BYTES:
            raise _invalid()
        try:
            with safe_cache_directory(self.root, create=True) as directory:
                temporary = '.factor-' + uuid4().hex
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(body)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except OSError:
            raise _invalid() from None
