"""Counted dated vendor reports, never independently verified listing history.

Names/market labels are vendor backfills, not historical ST evidence. No listing
intervals are inferred. Transport bounds/pacing/cancellation come from the shared
reader/client. No retries, alternate hosts, or live discovery are performed.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from app.services.market_diagnostics import source_failure_context
from app.services.market_universe import ExchangeSecurityMaster, UniverseDataError, _day

URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
SOURCE_ID = 'eastmoney_dated_universe'
VERSION = 1
PAGE_SIZE = 5000
MAX_PAGES = 20
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_CACHE_ENTRIES = 32
MAX_CACHE_TOTAL_BYTES = 128 * 1024 * 1024
COLUMNS = 'SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_MARKET,TRADE_DATE,CLOSE_PRICE,CHANGE_RATE'
LOG = logging.getLogger(__name__)


def _invalid(message, code='invalid_universe'):
    return UniverseDataError(message, code=code)


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')
    except (ValueError, OverflowError):
        raise _invalid('东方财富报告含非有限数值。', 'invalid_numeric') from None


def _integer(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise _invalid('东方财富分页计数无效。', 'invalid_count')
    return value


@contextmanager
def safe_cache_directory(root, *, create=False):
    """Yield owned directory fd; None yields None. Caller must not close it.

    All ancestors use O_NOFOLLOW. Missing directories may be created mode0700.
    Raw OS errors (including FileNotFoundError) and body exceptions propagate;
    the caller owns diagnostics, cancellation checks and error normalization.
    """
    if root is None:
        yield None
        return
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(os.path.abspath(root)).parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class EastmoneyDatedUniverse(ExchangeSecurityMaster):
    """Opt-in complete-report cache; corrupt hits fail without same-run refetch.

    Inherited streaming caps each raw/decoded response at25MiB, with cooperative
    120s request/600s report deadlines. Cache limits apply only to own files.
    """
    dated_report = True

    def __init__(self, client=None, *, cache_root=None, timeout=None, monotonic=None, now=None):
        super().__init__(client, timeout=timeout, monotonic=monotonic, now=now)
        self.cache_root = Path(cache_root) if cache_root is not None else None

    def _check_budget(self, request_deadline=None, status_code=None):
        check = getattr(self.client, 'check', None)
        if callable(check):
            check()
        super()._check_budget(request_deadline, status_code)

    @staticmethod
    def _params(day, page):
        return {'reportName': 'RPT_VALUEANALYSIS_DET', 'columns': COLUMNS,
                'pageSize': PAGE_SIZE, 'pageNumber': page, 'sortColumns': 'SECURITY_CODE',
                'sortTypes': 1, 'filter': f"(TRADE_DATE='{day}')"}

    @staticmethod
    @source_failure_context(stage='page_validation')
    def _page(payload, index, expected=None):
        if (not isinstance(payload, dict) or payload.get('success') is not True
                or type(payload.get('code')) is not int or payload['code'] != 0
                or not isinstance(payload.get('result'), dict)):
            raise _invalid('东方财富报告返回错误或结构无效。', 'invalid_page')
        result = payload['result']
        count = _integer(result.get('count'), PAGE_SIZE * MAX_PAGES)
        pages = _integer(result.get('pages'), MAX_PAGES)
        if pages != (count + PAGE_SIZE - 1) // PAGE_SIZE or not 1 <= index <= pages:
            raise _invalid('东方财富分页数量不一致。', 'invalid_page')
        if expected is not None and expected != (count, pages):
            raise _invalid('东方财富采集期间总数变化。', 'count_changed')
        rows = result.get('data')
        if not isinstance(rows, list) or len(rows) != min(PAGE_SIZE, count - (index - 1) * PAGE_SIZE):
            raise _invalid('东方财富分页不完整。', 'incomplete_page')
        return count, pages

    @source_failure_context(stage='rows_validation')
    def _validate(self, pages, day):
        if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
            raise _invalid('东方财富分页缺失。', 'invalid_page')
        expected, seen, rows, counts = None, set(), [], {'SH': 0, 'SZ': 0, 'BJ': 0}
        for index, payload in enumerate(pages, 1):
            expected = self._page(payload, index, expected)
            for raw in payload['result']['data']:
                self._check_budget()
                if not isinstance(raw, dict) or not set(COLUMNS.split(',')).issubset(raw):
                    raise _invalid('东方财富证券记录缺少必需字段。')
                code, identity = raw['SECURITY_CODE'], raw['SECUCODE']
                if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code)
                        or not isinstance(identity, str)
                        or identity not in (code + '.SH', code + '.SZ', code + '.BJ')):
                    raise _invalid('东方财富证券标识不一致。', 'invalid_symbol')
                if identity in seen:
                    raise _invalid('东方财富证券记录重复。', 'duplicate_symbol')
                seen.add(identity)
                if raw['TRADE_DATE'] not in (day, day + ' 00:00:00'):
                    raise _invalid('东方财富记录日期不符。', 'invalid_date')
                name, board = raw['SECURITY_NAME_ABBR'], raw['TRADE_MARKET']
                if not isinstance(name, str) or not name.strip() or name.strip() == '-':
                    raise _invalid('东方财富证券名称无效。', 'invalid_name')
                if not isinstance(board, str) or not board.strip():
                    raise _invalid('东方财富证券市场无效。', 'invalid_type')
                for key in ('CLOSE_PRICE', 'CHANGE_RATE'):
                    value = raw[key]
                    if value is not None and (type(value) not in (int, float)
                            or (isinstance(value, float) and not math.isfinite(value))
                            or (key == 'CLOSE_PRICE' and value < 0)):
                        raise _invalid('东方财富数值无效。', 'invalid_numeric')
                market = identity[-2:]
                counts[market] += 1
                if market == 'BJ':
                    continue
                rows.append({'date': day, 'symbol': market.lower() + code, 'code': code,
                    'name': name.strip(), 'exchange': 'SSE' if market == 'SH' else 'SZSE',
                    'security_type': 'cdr' if market == 'SH' and code.startswith('689') else 'stock',
                    'board': board, 'evidence': {'source_id': SOURCE_ID, 'target_date': day,
                    'report_date': day, 'name_basis': 'latest_vendor_backfill_not_historical_st',
                    'historical_st_verified': False, 'listing_dates_verified': False,
                    'membership_verified': False, 'raw': raw}})
        count, page_count = expected
        if len(pages) != page_count or len(seen) != count:
            raise _invalid('东方财富完整报告计数不匹配。', 'incomplete_page')
        if not counts['SH'] or not counts['SZ']:
            raise _invalid('东方财富报告缺少沪市或深市。', 'missing_market')
        return rows, counts

    def _cache_path(self, day):
        return self.cache_root / f'{SOURCE_ID}-v{VERSION}-{day}.json' if self.cache_root else None

    @contextmanager
    def _cache_directory(self, *, create=False):
        self._check_budget()
        with safe_cache_directory(self.cache_root, create=create) as directory:
            self._check_budget()
            yield directory
            self._check_budget()

    @staticmethod
    def _entry(directory, name):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise _invalid('缓存路径必须是普通文件，不能是符号链接。', 'cache_corrupt')
        return info

    @source_failure_context(stage='cache_read')
    def _cached(self, path):
        if path is None:
            return False
        try:
            with self._cache_directory() as directory:
                return self._entry(directory, path.name) is not None
        except FileNotFoundError:
            return False
        except OSError as error:
            if getattr(error, 'stop_collection', False):
                raise
            raise _invalid('缓存目录不可安全读取。', 'cache_corrupt') from None

    @source_failure_context(stage='cache_read')
    def _load(self, path, day):
        try:
            with self._cache_directory() as directory:
                self._entry(directory, path.name)
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'rb') as handle:
                        info = os.fstat(handle.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            raise OSError('not regular')
                        if info.st_size > MAX_CACHE_BYTES:
                            raise ValueError('oversized')
                        data = bytearray()
                        while True:
                            self._check_budget()
                            block = handle.read(min(1024 * 1024, MAX_CACHE_BYTES + 1 - len(data)))
                            self._check_budget()
                            data.extend(block)
                            if len(data) > MAX_CACHE_BYTES:
                                raise ValueError('oversized')
                            if not block:
                                break
                    envelope = json.loads(data)
                    if (envelope['source_id'] != SOURCE_ID or type(envelope['version']) is not int
                            or envelope['version'] != VERSION or envelope['date'] != day
                            or envelope['url'] != URL
                            or sha256(_encoded(envelope['pages'])).hexdigest() != envelope['sha256']):
                        raise ValueError('identity/digest')
                    self._check_budget()
                    self._validate(envelope['pages'], day)
                    self._check_budget()
                    return envelope['pages']
                except (ValueError, KeyError, TypeError, RecursionError) as error:
                    if (getattr(error, 'stop_collection', False)
                            or getattr(error, 'code', None) in ('catalogue_deadline', 'request_deadline')):
                        raise
                    current = self._entry(directory, path.name)
                    if current and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                        os.unlink(path.name, dir_fd=directory)
                    raise _invalid('日期报告缓存损坏；本次不重新请求。', 'cache_corrupt') from None
        except OSError as error:
            if getattr(error, 'stop_collection', False):
                raise
            raise _invalid('缓存文件不可安全读取。', 'cache_corrupt') from None

    @source_failure_context(stage='cache_write')
    def _save(self, path, day, pages):
        self._check_budget()
        if path is None:
            return
        content = _encoded({'source_id': SOURCE_ID, 'version': VERSION, 'date': day,
                            'url': URL, 'sha256': sha256(_encoded(pages)).hexdigest(), 'pages': pages})
        self._check_budget()
        if len(content) > MAX_CACHE_BYTES:
            raise _invalid('完整报告超出缓存大小限制。', 'response_too_large')
        try:
            with self._cache_directory(create=True) as directory:
                self._entry(directory, path.name)
                temporary = f'.{SOURCE_ID}-{uuid4().hex}'
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        for start in range(0, len(content), 1024 * 1024):
                            self._check_budget()
                            handle.write(content[start:start + 1024 * 1024])
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._check_budget()
                    self._entry(directory, path.name)
                    os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    self._check_budget()
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                files = []
                for name in os.listdir(directory):
                    self._check_budget()
                    if re.fullmatch(rf'{SOURCE_ID}-v{VERSION}-\d{{4}}-\d{{2}}-\d{{2}}\.json', name):
                        info = self._entry(directory, name)
                        if info:
                            files.append((name, info))
                files.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
                total = 0
                for index, (name, info) in enumerate(files):
                    self._check_budget()
                    total += info.st_size
                    if index >= MAX_CACHE_ENTRIES or total > MAX_CACHE_TOTAL_BYTES:
                        self._entry(directory, name)
                        os.unlink(name, dir_fd=directory)
        except OSError as error:
            if getattr(error, 'stop_collection', False):
                raise
            raise _invalid('缓存文件不可安全写入。', 'cache_corrupt') from None

    @source_failure_context(source_id=SOURCE_ID, stage='catalogue_validation')
    def fetch(self, day):
        day = _day(day).isoformat()
        started = self._monotonic()
        LOG.info('Market dated universe start source=%s date=%s', SOURCE_ID, day)
        with self._catalogue_budget():
            path, requests = self._cache_path(day), []
            cached = self._cached(path)
            LOG.info('Market dated universe cache source=%s date=%s hit=%s', SOURCE_ID, day, cached)
            if cached:
                pages = self._load(path, day)
            else:
                pages, expected = [], None
                for index in range(1, MAX_PAGES + 1):
                    payload = self._json(URL, self._params(day, index), requests)
                    expected = self._page(payload, index, expected)
                    pages.append(payload)
                    if len(_encoded(pages)) > MAX_CACHE_BYTES:
                        raise _invalid('完整报告超出大小限制。', 'response_too_large')
                    LOG.info('Market dated universe page source=%s date=%s page=%d pages=%d rows=%d expected=%d',
                             SOURCE_ID, day, index, expected[1], len(payload['result']['data']), expected[0])
                    if index == expected[1]:
                        break
            rows, counts = self._validate(pages, day)
            digest = sha256(_encoded(pages)).hexdigest()
            if not cached:
                self._save(path, day, pages)
            self._check_budget()
            LOG.info('Market dated universe coverage source=%s date=%s selected=%d sh=%d sz=%d bj=%d pages=%d elapsed=%.3f',
                     SOURCE_ID, day, len(rows), counts['SH'], counts['SZ'], counts['BJ'], len(pages),
                     self._monotonic() - started)
            return {'date': day, 'rows': rows, 'expected_count': len(rows),
                'scope': 'SSE_SZSE_A_SHARE_CDR', 'source': '东方财富日期报告',
                'source_has_trading_date': True, 'evidence': {
                'source_id': SOURCE_ID, 'evidence_version': VERSION, 'report_date_verified': True,
                'report_pages_complete': True, 'source_counts_verified': True,
                'report_count': sum(counts.values()), 'source_counts': counts, 'report_pages': len(pages),
                'membership_verified': False, 'listing_dates_verified': False,
                'membership_basis': 'eastmoney_complete_dated_report', 'target_date': day,
                'report_digest': digest, 'name_basis': 'latest_vendor_backfill_not_historical_st',
                'historical_st_verified': False, 'cache_hit': cached},
                'limitations': ['报告完整性不等于独立验证历史成员资格；供应商回填名称不证明历史 ST 状态。'],
                'raw': {'requests': requests}}
