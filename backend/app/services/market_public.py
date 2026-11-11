"""Dated public-market inputs for the original JoinQuant calculation.

Complete dated reports propose membership; independent trade/pause and official
category counts audit each date. Public
TDX daily archives supply prices and turnover. Corporate-action records select
windows requiring Sina adjustment factors. Missing bars require affirmative
full-session suspension evidence; they are never inferred from absence.
"""
from bisect import bisect_right
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
import logging

from app.services.growth_dividend_source import fetch_calendar
from app.services.market_diagnostics import copy_source_failure, source_failure_context
from app.services.market_eastmoney_universe import safe_cache_directory
from app.services.market_methodology import JOINQUANT_HEAT_METHOD
from app.services.market_history import (audit_historical_membership, equity_namespace,
    DATED_SOURCE, DATED_EVIDENCE_VERSION, DATED_MEMBERSHIP_BASIS)
from app.services.market_sina import (
    MarketDependencyError, SinaDataError, SinaMarketProvider, SinaRateLimitError, _bars, _day, _number,
    _symbol, calculate_joinquant_quote, verified_membership_sessions,
)

LOG = logging.getLogger(__name__)
SOURCE_ID = 'public_market'
TENCENT_QFQ_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
FACTOR_URL = 'https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js'


def parse_factors(text, symbol):
    """Accept Sina's data assignment, not executable remote JavaScript."""
    import json
    symbol = _symbol(symbol)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S).strip()
    match = re.fullmatch(r'var\s+' + re.escape(symbol) + r'qfq\s*=\s*(\{.*\})\s*;?', text, re.S)
    if not match:
        raise SinaDataError('复权因子结构无效。', code='adjustment_unavailable')
    try:
        payload = json.loads(match.group(1))
        rows = payload['data']
        if not re.fullmatch(r'\d+', str(payload['total'])):
            raise ValueError()
        count = int(payload['total'])
        if type(payload['total']) is bool or not isinstance(rows, list) or count != len(rows):
            raise ValueError()
        if count == 0:
            raise SinaDataError('新浪未提供该证券复权因子。', code='adjustment_empty')
        result = [(_day(row['d']), _number(row['f'], positive=True)) for row in rows]
        if len({day for day, _ in result}) != len(result):
            raise ValueError()
        return sorted(result)
    except SinaDataError as error:
        if error.code == 'adjustment_empty':
            raise
        raise SinaDataError('复权因子缺失或不完整。', code='adjustment_unavailable') from None
    except (ValueError, KeyError, TypeError):
        raise SinaDataError('复权因子缺失或不完整。', code='adjustment_unavailable') from None


class PublicMarketProvider(SinaMarketProvider):
    """One provider orchestration with explicit, replaceable source adapters.

    Injected sources make the schema checks independently testable. All default
    HTTP requests use the collector's shared pacing/cancellation client. Source
    errors and access limits propagate; no host-switch or synthetic fallback.
    """

    source_id = SOURCE_ID

    def __init__(self, client=None, *, cache_root=None, master=None, archives=None,
                 actions=None, suspensions=None, calendar_loader=None, factor_url=FACTOR_URL, tencent_qfq_url=TENCENT_QFQ_URL, counts=None, listing_metadata=None):
        super().__init__(client)
        if master is None:
            from app.services.market_eastmoney_universe import EastmoneyDatedUniverse
            master = EastmoneyDatedUniverse(self.client, cache_root=cache_root)
        if archives is None:
            from app.services.market_bulk import TdxDailyArchives
            archives = TdxDailyArchives(self.client, cache_root=cache_root)
        if suspensions is None:
            from app.services.market_suspensions import PublicSuspensions
            suspensions = PublicSuspensions(self.client)
        if counts is None:
            from app.services.market_counts import ExchangeDailyCounts
            counts = ExchangeDailyCounts(self.client)
        self.counts = counts
        self._dated_master = getattr(master, 'dated_report', False) is True
        self._audit_cache = Path(cache_root) / 'eastmoney-audited-v1' if cache_root is not None and self._dated_master else None
        self._membership_windows, self._listing_outcomes = {}, {}
        self._listing_transport_error = None
        if listing_metadata is None and self._dated_master:
            from app.services.market_eastmoney_listing import EastmoneyListingMetadata
            listing_metadata = EastmoneyListingMetadata(self.client, cache_root=cache_root)
        self.listing_metadata = listing_metadata
        self.master, self.archives, self.actions, self.suspensions = master, archives, actions, suspensions
        self.calendar_loader = calendar_loader or fetch_calendar
        self.factor_url = factor_url
        self.tencent_qfq_url = tencent_qfq_url
        self._calendar_days = None
        self._daily, self._contexts, self._factors = {}, {}, {}
        self._action_data = None
        self._pause_data = {}
        self._wanted_symbols = None
        self._raw_equities = {}
        self._archive_evidence = {}

    @staticmethod
    def _dependency_call(code, operation, *args):
        try:
            return operation(*args)
        except (SinaRateLimitError, MarketDependencyError):
            raise
        except Exception as error:
            # Cancellation is not an unavailable input and must never be
            # swallowed by provider result normalization.
            if getattr(error, 'stop_collection', False):
                raise
            if getattr(error, 'status_code', None) in (403, 429):
                raise copy_source_failure(error, SinaRateLimitError(error.status_code)) from None
            original = getattr(error, 'code', None)
            if original == 'cache_corrupt':
                code = 'dated_report_cache_invalid'
            if original in ('invalid_tdx_archive', 'snapshot_invalid', 'dated_report_invalid', 'dated_report_cache_invalid'):
                code = original
            raise copy_source_failure(error, MarketDependencyError(code)) from None

    @staticmethod
    def heat_capability():
        # This describes supported validation, not a waiver of per-stock checks.
        return {'methodology': JOINQUANT_HEAT_METHOD, 'verified': True,
                'price_basis': 'qfq', 'denominator': 'eligible'}

    @staticmethod
    def historical_capability():
        # fetch_universe still rejects any date whose membership is unproved.
        return {'verified': True, 'date_specific_validation_required': True}

    def prepare_snapshot(self, snapshot):
        wanted = {row['symbol'] for row in snapshot['rows']}
        if wanted != self._wanted_symbols and not self._dated_master:
            # One run may visit several point-in-time pools. A filtered memory
            # cache cannot silently hide a since-delisted member in an older pool.
            self._daily.clear()
            self._raw_equities.clear()
            self._archive_evidence.clear()
        self._wanted_symbols = wanted

    def _sessions(self, day):
        return self._dependency_call('calendar_unavailable', self._load_sessions, day)

    def _load_sessions(self, day):
        index = self._calendar(day)
        if index < 19:
            raise MarketDependencyError('calendar_incomplete')
        return self._calendar_days[index - 19:index + 1]

    def snapshot_compatible(self, snapshot):
        if not self._dated_master:
            return True
        try:
            evidence = snapshot['universe_evidence']
            return (snapshot['source'] == SOURCE_ID and evidence['source_id'] == DATED_SOURCE
                    and type(evidence['evidence_version']) is int and evidence['evidence_version'] == DATED_EVIDENCE_VERSION
                    and evidence['membership_basis'] == DATED_MEMBERSHIP_BASIS
                    and evidence['historical_reconstruction_verified'] is True
                    and evidence['target_date'] == snapshot['date']
                    and re.fullmatch(r'[0-9a-f]{64}', evidence['audit_digest']) is not None
                    and isinstance(snapshot['rows'], list) and bool(snapshot['rows'])
                    and all(row['date'] == snapshot['date']
                            and row['listing']['evidence']['source_id'] == DATED_SOURCE
                            and row['universe_evidence']['source_id'] == DATED_SOURCE
                            and row['universe_evidence']['audit_digest'] == evidence['audit_digest']
                            and row['universe_evidence']['report_digest'] == evidence['report_digest']
                            for row in snapshot['rows']))
        except (KeyError, TypeError, AttributeError):
            return False

    def _calendar(self, day):
        if self._calendar_days is None:
            days = [_day(value) for value in self._dependency_call('calendar_unavailable', self.calendar_loader, self.client)]
            if not days or days != sorted(set(days)):
                raise MarketDependencyError('calendar_unavailable')
            self._calendar_days = days
        try:
            return self._calendar_days.index(day)
        except ValueError:
            raise MarketDependencyError('calendar_incomplete') from None

    @staticmethod
    def _audit_bytes(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

    @classmethod
    def _audit_identity(cls, inputs):
        # Stable semantics, never delivery metadata/cache-hit/capture timestamps.
        master = inputs['master']
        body = {'source_id': DATED_SOURCE, 'version': DATED_EVIDENCE_VERSION,
                'date': master['date'], 'report_digest': master['evidence']['report_digest'],
                'archive_digest': master['source_archive_evidence']['sha256'],
                'members': sorted(row['symbol'] for row in master['rows']),
                'official_counts': inputs['official']['counts'],
                'observations': [{key: row[key] for key in ('symbol', 'date', 'close', 'volume', 'amount', 'paused')}
                    for row in sorted(inputs['rows'], key=lambda item: item['symbol'])]}
        return sha256(cls._audit_bytes(body)).hexdigest()

    def _audit_path(self, day):
        return self._audit_cache / (str(day) + '-eastmoney-v1.json') if self._audit_cache is not None else None

    def _check_cancelled(self):
        check = getattr(self.client, 'check', None)
        if callable(check):
            check()

    @source_failure_context(source_id=DATED_SOURCE, stage='cache_read')
    def _load_audit(self, day):
        path = self._audit_path(day)
        if path is None:
            return None
        self._check_cancelled()
        try:
            with safe_cache_directory(path.parent) as directory:
                try:
                    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    return None
                with os.fdopen(fd, 'rb') as stream:
                    original = os.fstat(stream.fileno())
                    if not stat.S_ISREG(original.st_mode):
                        raise OSError('Non-regular cache entry')
                    body = stream.read(32 * 1024 * 1024 + 1)
                self._check_cancelled()
                try:
                    if len(body) > 32 * 1024 * 1024 or original.st_size > 32 * 1024 * 1024:
                        raise ValueError()
                    payload = json.loads(body)
                    inputs = payload['inputs']
                    if (payload['source_id'] != DATED_SOURCE or type(payload['version']) is not int
                            or payload['version'] != DATED_EVIDENCE_VERSION or payload['date'] != str(day)
                            or sha256(self._audit_bytes(inputs)).hexdigest() != payload['sha256']
                            or inputs['master']['date'] != str(day)):
                        raise ValueError()
                    evidence = audit_historical_membership(inputs['master'], inputs['rows'], inputs['raw_equities'],
                                                            inputs['suspensions'], inputs['official'])
                    if evidence.get('source_id') != DATED_SOURCE:
                        raise ValueError()
                    evidence['audit_digest'] = self._audit_identity(inputs)
                    self._check_cancelled()
                    return self._snapshot_result(day, inputs['rows'], evidence)
                except (ValueError, KeyError, TypeError, RecursionError):
                    try:
                        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    if current is not None and (original.st_dev, original.st_ino) == (current.st_dev, current.st_ino):
                        os.unlink(path.name, dir_fd=directory)
                    raise MarketDependencyError('dated_report_cache_invalid') from None
        except FileNotFoundError:
            return None
        except OSError:
            raise MarketDependencyError('dated_report_cache_invalid') from None

    @source_failure_context(source_id=DATED_SOURCE, stage='cache_write')
    def _save_audit(self, day, inputs):
        self._check_cancelled()
        path = self._audit_path(day)
        if path is not None:
            body = self._audit_bytes({'source_id': DATED_SOURCE, 'version': DATED_EVIDENCE_VERSION,
                'date': str(day), 'sha256': sha256(self._audit_bytes(inputs)).hexdigest(), 'inputs': inputs})
            if len(body) > 32 * 1024 * 1024:
                raise MarketDependencyError('dated_report_cache_invalid')
            try:
                with safe_cache_directory(path.parent, create=True) as directory:
                    try:
                        if not stat.S_ISREG(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                            raise OSError('Non-regular cache entry')
                    except FileNotFoundError:
                        pass
                    temp = '.audit-' + uuid.uuid4().hex
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(body)
                            stream.flush()
                            os.fsync(stream.fileno())
                        self._check_cancelled()
                        os.replace(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    finally:
                        try:
                            os.unlink(temp, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                    files = []
                    with os.scandir(directory) as entries:
                        for index, entry in enumerate(entries):
                            self._check_cancelled()
                            if index >= 96:
                                raise MarketDependencyError('dated_report_cache_invalid')
                            if re.fullmatch(r'\d{4}-\d{2}-\d{2}-eastmoney-v1\.json', entry.name):
                                info = entry.stat(follow_symlinks=False)
                                if stat.S_ISREG(info.st_mode):
                                    files.append((info.st_mtime_ns, entry.name, info))
                    files.sort(reverse=True)
                    total = 0
                    for index, (_, name, info) in enumerate(files):
                        total += info.st_size
                        if (index >= 32 or total > 256 * 1024 * 1024) and name != path.name:
                            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                            if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                                os.unlink(name, dir_fd=directory)
            except OSError:
                raise MarketDependencyError('dated_report_cache_invalid') from None
        return self._audit_identity(inputs)

    def _snapshot_result(self, day, rows, evidence):
        result = {'date': str(day), 'expected_count': len(rows), 'rows': rows,
                  'source': SOURCE_ID, 'scope': 'sse_szse_stock_and_cdr',
                  'source_has_trading_date': True, 'dated_snapshot_verified': True,
                  'date_verified_by': 'public_daily_archive', 'universe_evidence': evidence,
                  'fetched_at': datetime.now(timezone.utc).isoformat()}
        for row in rows:
            row['universe_evidence'] = {key: evidence.get(key) for key in
                ('source_id', 'evidence_version', 'membership_basis', 'target_date', 'report_digest',
                 'audit_digest', 'historical_reconstruction_verified', 'joinquant_universe_directly_verified')}
        if day not in self._contexts and len(self._contexts) >= 24:
            self._contexts.pop(next(iter(self._contexts)))
        self._contexts[day] = result
        return result

    @source_failure_context(source_id=DATED_SOURCE, stage='window_validation')
    def _dated_window(self, symbol, day, sessions):
        if day not in self._membership_windows:
            records = []
            for current in sessions:
                snapshot = self._contexts.get(current) or self._dependency_call(
                    'dated_membership_window_unverified', self.fetch_universe, current)
                if not self.snapshot_compatible(snapshot):
                    raise MarketDependencyError('dated_membership_window_unverified')
                evidence = snapshot['universe_evidence']
                records.append({'date': str(current), 'audited': True,
                    'symbols': {row['symbol'] for row in snapshot['rows']},
                    'report_digest': evidence['report_digest'], 'audit_digest': evidence['audit_digest']})
            if len(self._membership_windows) >= 4:
                self._membership_windows.pop(next(iter(self._membership_windows)))
            self._membership_windows[day] = records
        window = {'symbol': symbol, 'target_date': str(day), 'source_id': DATED_SOURCE,
                  'evidence_version': DATED_EVIDENCE_VERSION, 'sessions': [
                      {key: value for key, value in item.items() if key != 'symbols'} |
                      {'present': symbol in item['symbols']} for item in self._membership_windows[day]]}
        present = [item['present'] for item in window['sessions']]
        if not all(present) and present[-1] and all(present[present.index(True):]):
            metadata = self._ipo_metadata(symbol, day)
            window['ipo_evidence'] = {key: metadata.get(key) for key in
                ('symbol', 'exchange', 'ipo_date', 'source_id', 'evidence_version', 'request_secid',
                 'verified', 'source_digest', 'source_schema', 'raw_response')}
        verified_membership_sessions(symbol, day, sessions, window)
        return window

    def _ipo_metadata(self, symbol, day):
        if self._listing_transport_error is not None:
            raise self._listing_transport_error
        key = (symbol, day)
        if key not in self._listing_outcomes:
            if len(self._listing_outcomes) >= 128:
                self._listing_outcomes.pop(next(iter(self._listing_outcomes)))
            try:
                if self.listing_metadata is None:
                    raise SinaDataError('缺少上市日期来源。', code='dated_membership_gap')
                self._listing_outcomes[key] = self.listing_metadata.fetch(symbol, day)
            except Exception as error:
                if getattr(error, 'stop_collection', False):
                    raise
                if getattr(error, 'status_code', None) in (403, 429):
                    raise copy_source_failure(error, SinaRateLimitError(error.status_code)) from None
                import httpx
                if (isinstance(error, (httpx.HTTPError, MarketDependencyError)) or
                        getattr(error, 'code', None) in ('network_error', 'request_deadline', 'catalogue_deadline')):
                    self._listing_transport_error = copy_source_failure(error, MarketDependencyError('dated_membership_window_unverified'))
                    raise self._listing_transport_error from None
                self._listing_outcomes[key] = copy_source_failure(error,
                    SinaDataError('单证券上市日期尚未核实。', code='dated_membership_gap'))
        result = self._listing_outcomes[key]
        if isinstance(result, Exception):
            raise result
        if not isinstance(result, dict):
            raise SinaDataError('单证券上市日期尚未核实。', code='dated_membership_gap')
        return result

    def _daily_rows(self, day):
        try:
            return self._dependency_call('snapshot_invalid', self._load_daily_rows, day)
        except MarketDependencyError as error:
            # Only rejected archive bytes/payloads invalidate this date's cache.
            # Calendar/master/connection failures must not discard good input.
            if error.code in ('snapshot_invalid', 'invalid_tdx_archive'):
                invalidate = getattr(self.archives, 'invalidate', None)
                if callable(invalidate):
                    invalidate(day)
                self._daily.pop(day, None)
            raise

    def _load_daily_rows(self, day):
        if day not in self._daily:
            if self._dated_master and len(self._daily) >= 24:
                stale = next(iter(self._daily))
                for cache in (self._daily, self._raw_equities, self._archive_evidence):
                    cache.pop(stale, None)
            data = self._dependency_call('source_unavailable', self.archives.fetch, day)
            if data.get('date') != str(day) or data.get('verified') is not True:
                raise MarketDependencyError('snapshot_invalid')
            rows = data['rows']
            rows = list(rows.values()) if isinstance(rows, dict) else rows
            by_symbol = {row['symbol']: row for row in rows}
            if len(by_symbol) != len(rows) or any(row.get('date') != str(day) for row in rows):
                raise MarketDependencyError('snapshot_invalid')
            self._archive_evidence[day] = {'date': str(day), 'sha256': data.get('source_digest'),
                                           'source_url': data.get('source_url'), 'parsed_records': len(by_symbol)}
            self._raw_equities[day] = {symbol: row for symbol, row in by_symbol.items()
                                       if equity_namespace(symbol) or (self._wanted_symbols is not None and symbol in self._wanted_symbols)}
            LOG.info('Market source archive date=%s source=tdx records=%d selected=%d',
                     day, len(by_symbol), len(set(by_symbol) & self._wanted_symbols) if self._wanted_symbols is not None else len(by_symbol))
            self._daily[day] = {symbol: row for symbol, row in by_symbol.items()
                                if self._dated_master or self._wanted_symbols is None or symbol in self._wanted_symbols}
        return self._daily[day]

    def _pauses(self, start, end):
        key = (start, end)
        if key not in self._pause_data:
            if self._dated_master and len(self._pause_data) >= 32:
                self._pause_data.pop(next(iter(self._pause_data)))
            self._pause_data[key] = self._dependency_call('paused_unknown', self.suspensions.fetch, start, end)
        return self._pause_data[key]

    def _pause_evidence(self, symbol, day, start, end):
        data = self._pauses(start, end)
        intervals = data.get('intervals', {}).get(symbol, [])
        for interval in intervals:
            if ((interval.get('full_day') is True or
                 (interval.get('paused') == 1 and interval.get('evidence', {}).get('full_day_verified') is True))
                    and _day(interval['start']) <= day
                    and (interval.get('end') is None or day <= _day(interval['end']))):
                return interval
        raise SinaDataError('缺失行情没有已核实的全天停牌证据。', code='paused_unknown')

    @staticmethod
    def _listing(member, day):
        start = _day(member['listed_from'])
        end = _day(member['listed_until']) if member.get('listed_until') else date.max
        if member.get('listed_until') and member.get('listed_until_exclusive') is True:
            end -= timedelta(days=1)
        if not start <= day <= end:
            raise SinaDataError('证券不在目标日上市区间。', code='listing_date_mismatch')
        return start, end

    def _prior_close(self, symbol, before):
        # Used only for a positively proved suspension at the first window day.
        # There is a strict finite history bound; an older missing seed blocks.
        from app.services.market_sina import KLINE_URL
        payload = self._request(KLINE_URL, {'symbol': symbol, 'scale': '240',
                                          'ma': 'no', 'datalen': '1970'}, [])
        candidates = [row for row in _bars(payload) if _day(row['date']) < before]
        if not candidates:
            raise SinaDataError('停牌窗口缺少此前有效收盘价。', code='incomplete_heat_window')
        row = candidates[-1]
        return _day(row['date']), _number(row['close'], positive=True)

    def _historical_counts(self, day):
        try:
            return self.counts.fetch(day)
        except (SinaRateLimitError, MarketDependencyError):
            raise
        except SinaDataError as error:
            if error.code == 'unsupported_historical_schema':
                raise
            raise SinaDataError('目标日交易所分类统计未通过校验。', code='historical_count_unavailable') from error

    def fetch_universe(self, day):
        try:
            return self._fetch_universe(day)
        except (MarketDependencyError, SinaRateLimitError):
            raise
        except (SinaDataError, ValueError):
            invalidate = getattr(self.archives, 'invalidate', None)
            if callable(invalidate):
                invalidate(_day(day))
            self._daily.pop(_day(day), None)
            raise

    def _fetch_universe(self, day):
        day = _day(day)
        if self._dated_master:
            self._calendar(day)
            cached = self._load_audit(day)
            if cached is not None:
                return cached
        sessions = [day] if self._dated_master else self._sessions(day)
        master = self._dependency_call('dated_report_invalid' if self._dated_master else 'universe_incomplete', self.master.fetch, day)
        LOG.info('Market source master date=%s source=%s securities=%d historical_verified=%s',
                 day, DATED_SOURCE if self._dated_master else 'exchange', len(master.get('rows', [])), master.get('evidence', {}).get('membership_verified') is True)
        if (master.get('date') != str(day) or not master.get('rows')
                or master.get('expected_count') != len(master['rows'])):
            raise SinaDataError('交易所证券主表不完整。', code='universe_incomplete')
        historical_audit = self._dated_master or master.get('evidence', {}).get('membership_verified') is not True
        if historical_audit and not self._dated_master and any(master.get('evidence', {}).get(key) is not True for key in
                                    ('source_counts_verified', 'listing_dates_verified', 'current_catalogue_verified')):
            raise SinaDataError('历史主表来源和上市区间证据不完整。', code='universe_coverage_unverified')
        self.prepare_snapshot(master)
        current = self._daily_rows(day)
        rows = []
        for member in master['rows']:
            symbol = _symbol(member['symbol'])
            if not self._dated_master:
                self._listing(member, day)
            observed = current.get(symbol)
            pause = None
            if (observed is None or _number(observed.get('volume', 0)) == 0):
                pause = self._pause_evidence(symbol, day, sessions[0], day)
                if observed is not None and _number(observed.get('amount', 0)) != 0:
                    raise SinaDataError('停牌状态与成交额冲突。', code='snapshot_quote_mismatch')
                if observed is None or _number(observed.get('close', 0)) == 0:
                    _, previous = self._prior_close(symbol, day)
                    observed = {'close': str(previous), 'volume': '0', 'amount': '0'}
            elif (_number(observed.get('close'), positive=True) <= 0
                  or _number(observed.get('amount'), positive=True) <= 0):
                raise SinaDataError('有效交易行情不完整。', code='snapshot_invalid')
            rows.append({'symbol': symbol, 'name': member['name'], 'date': str(day),
                         'close': str(observed['close']), 'volume': str(observed['volume']),
                         'amount': str(observed['amount']), 'paused': 1 if pause else 0,
                         'listing': member, 'pause_evidence': pause,
                         'source_observation': {key: observed.get(key) for key in ('source_url', 'source_digest')}})
        if len({row['symbol'] for row in rows}) != len(rows):
            raise SinaDataError('目标日证券代码重复。', code='duplicate_symbol')
        universe_evidence = master['evidence']
        if historical_audit:
            inputs = {'master': master | {'source_archive_evidence': self._archive_evidence[day]},
                      'rows': rows, 'raw_equities': self._raw_equities[day],
                      'suspensions': self._pauses(sessions[0], day), 'official': self._historical_counts(day)}
            universe_evidence = audit_historical_membership(inputs['master'], rows, inputs['raw_equities'],
                                                            inputs['suspensions'], inputs['official'])
            if self._dated_master:
                universe_evidence['audit_digest'] = self._save_audit(day, inputs)
            LOG.info('Market historical audit date=%s traded=%d paused=%d total=%d', day,
                     universe_evidence['traded_identity_count'], universe_evidence['paused_identity_count'], len(rows))
        return self._snapshot_result(day, rows, universe_evidence)

    def _window_actions(self, symbol, sessions):
        if self._action_data is None:
            self._action_data = self._dependency_call('adjustment_unavailable', self.actions.fetch)
        data = self._action_data
        if data.get('verified') is not True:
            raise MarketDependencyError('adjustment_unavailable')
        if _day(data['coverage_through']) < sessions[-1]:
            raise MarketDependencyError('adjustment_unavailable')
        return [row for row in data['rows'] if row['symbol'] == symbol
                and row['category'] == 1 and sessions[0] < _day(row['date']) <= sessions[-1]]

    def _adjust(self, symbol, first, actions):
        if actions == []:
            return (lambda day, close: close), 'constant-scale equivalence; no action in complete source window'
        if symbol not in self._factors:
            requests = []
            text = self._request_text(self.factor_url.format(symbol=symbol), None, requests)
            self._factors[symbol] = parse_factors(text, symbol)
        factors = self._factors[symbol]
        dates = [day for day, _ in factors]
        if dates[0] > first or any(_day(action['date']) not in dates for action in (actions or [])):
            raise SinaDataError('复权因子未覆盖窗口或已知除权日。', code='adjustment_unavailable')
        def adjusted(day, close):
            index = bisect_right(dates, day) - 1
            if index < 0:
                raise SinaDataError('复权因子缺少起始值。', code='adjustment_unavailable')
            return close / factors[index][1]
        return adjusted, 'Sina dated qfq factors (full precision)'

    def _verify_identity_window(self, symbol, sessions, snapshot):
        """Restricted empty-factor evidence, NOT generic vendor equivalence.

        Every explicit Tencent qfq close must equal an independently dated raw
        close. A full displayed quantum (not undocumented half-tick rounding)
        bounds each qfq value, and near-MA results remain unverified. Any changing
        adjustment, missing date or unclear volume units rejects this route.
        """
        payload = self._request(self.tencent_qfq_url, {
            'param': f'{symbol},day,{sessions[0]},{sessions[-1]},40,qfq',
        }, [])
        try:
            if payload['code'] != 0:
                raise ValueError()
            values = payload['data'][symbol]['qfqday']  # never accept raw "day"
            if not isinstance(values, list) or not values:
                raise ValueError()
            observed = {}
            for value in values:
                if not isinstance(value, list) or len(value) < 6:
                    raise ValueError()
                day = _day(value[0])
                if day in observed:
                    raise ValueError()
                observed[day] = value
            prices, errors, evidence_rows = [], [], []
            for day in sessions:
                value = observed[day]
                raw = self._daily_rows(day).get(symbol)
                if raw is None or _number(raw['volume'], positive=True) <= 0:
                    raise ValueError()
                close = _number(value[2], positive=True)
                if close != _number(raw['close'], positive=True):
                    raise ValueError()
                if day == sessions[-1] and _number(value[5]) != _number(snapshot['volume']):
                    raise ValueError()
                quantum = Decimal(1).scaleb(close.as_tuple().exponent)
                prices.append(close)
                errors.append(quantum)
                evidence_rows.append({'date': str(day), 'qfq_close': str(close),
                                      'raw_close': raw['close'], 'quantum': str(quantum)})
            mean = sum(prices) / 20
            bound = (19 * errors[-1] + sum(errors[:-1])) / 20
            tolerance = max(abs(prices[-1]), abs(mean)) * Decimal('1e-12')
            if abs(prices[-1] - mean) <= bound + tolerance:
                raise SinaDataError('复权显示精度可能改变均线判断。', code='adjustment_precision_unverified')
            return {'source': 'tencent_explicit_qfq', 'rows': evidence_rows,
                    'difference_error_bound': str(bound),
                    'scope': 'only_this_complete_raw_equal_qfq_window',
                    'all_scenarios_joinquant_equivalence': False}
        except MarketDependencyError:
            raise
        except SinaDataError as error:
            if error.code == 'adjustment_precision_unverified':
                raise
            raise SinaDataError('双源复权窗口仍需核验。', code='adjustment_unavailable') from None
        except (ValueError, KeyError, TypeError):
            raise SinaDataError('双源复权窗口不完整或存在调整。', code='adjustment_unavailable') from None

    def fetch_quote(self, symbol, day, *, snapshot=None, datalen=1):
        symbol, day = _symbol(symbol), _day(day)
        result = {'symbol': symbol, 'day': str(day), 'status': 'transient_failure',
                  'error': 'quote_unavailable', 'source': SOURCE_ID}
        current = day
        try:
            if not isinstance(snapshot, dict) or snapshot.get('symbol') != symbol or snapshot.get('date') != str(day):
                raise SinaDataError('目标日快照缺失。', code='snapshot_invalid')
            sessions = self._sessions(day)
            membership_window = None
            listed_from = listed_until = None
            if self._dated_master:
                membership_window = self._dated_window(symbol, day, sessions)
                relevant = sorted(verified_membership_sessions(symbol, day, sessions, membership_window))
            else:
                listed_from, listed_until = self._listing(snapshot['listing'], day)
                relevant = [value for value in sessions if listed_from <= value <= listed_until]
            evidence = {'calendar_verified': True, 'listing_verified': not self._dated_master,
                        'paused_verified': True, 'prices_verified': True, 'universe_verified': True,
                        'price_basis': 'qfq', 'adjustment_basis': 'excluded without MA20',
                        'sources': [DATED_SOURCE if self._dated_master else 'exchange_master', 'tdx_daily', 'public_suspension_intervals'],
                        'universe_basis': snapshot.get('universe_evidence', {}).get('membership_basis', 'verified_snapshot')}
            if membership_window is not None:
                evidence.update(dated_membership_verified=True, membership_window=membership_window)
                if membership_window.get('ipo_evidence') is not None:
                    evidence['sources'].append('eastmoney_listing_metadata')
            # Current full-day pauses and <20-session IPOs cannot enter the heat
            # denominator. Their exclusion needs no invented adjusted MA value.
            if snapshot.get('paused') == 1 or len(relevant) < 20:
                paused = snapshot.get('paused') == 1
                if paused:
                    self._pause_evidence(symbol, day, sessions[0], day)
                return result | {'date': str(day), 'close': snapshot['close'], 'volume': snapshot['volume'],
                    'qfq_close': snapshot['close'], 'ma20': None, 'ma20_qfq': None,
                    'status': 'paused' if paused else 'insufficient_history', 'error': None,
                    'paused': int(paused), 'eligible': False, 'above': False,
                    'history_count': len(relevant), 'methodology': JOINQUANT_HEAT_METHOD,
                    'verified': True, 'price_basis': 'qfq', 'denominator': 'eligible',
                    'evidence': evidence, 'raw': {'listing': snapshot['listing'],
                                               'exclusion_only': True}}
            # An optional independently verified event service may prove a
            # constant scale. The public GBBQ package alone has no as-of coverage
            # attestation, so production defaults to per-stock Sina factors.
            actions = self._window_actions(symbol, sessions) if self.actions is not None else None
            identity_evidence = None
            try:
                adjusted, basis = self._adjust(symbol, sessions[0], actions)
            except SinaDataError as error:
                if error.code != 'adjustment_empty':
                    raise
                identity_evidence = self._verify_identity_window(symbol, sessions, snapshot)
                adjusted = lambda current, close: close
                basis = 'restricted dual-source raw/qfq equality, outside displayed-precision error bound'
            evidence['adjustment_basis'] = basis
            if identity_evidence is not None:
                evidence['dual_source_window'] = identity_evidence
            rows, previous = [], None
            for current in sessions:
                raw = self._daily_rows(current).get(symbol)
                active = raw is not None and _number(raw.get('volume', 0)) > 0
                if active:
                    close = adjusted(current, _number(raw['close'], positive=True))
                    if _number(raw['amount'], positive=True) <= 0:
                        raise SinaDataError('成交额缺失。', code='snapshot_invalid')
                else:
                    self._pause_evidence(symbol, current, sessions[0], day)
                    if raw is not None and _number(raw.get('amount', 0)) != 0:
                        raise SinaDataError('停牌与成交额冲突。', code='snapshot_quote_mismatch')
                    if previous is None:
                        prior_day, prior = self._prior_close(symbol, current)
                        close = adjusted(prior_day, prior)
                    else:
                        close = previous
                rows.append({'date': str(current), 'close_qfq': str(close), 'paused': 0 if active else 1})
                previous = close
            rows[-1].update(raw_close=snapshot['close'], volume=snapshot['volume'])
            quote = calculate_joinquant_quote(symbol, day, calendar=sessions, rows=rows,
                        listed_from=listed_from, listed_until=listed_until, evidence=evidence, snapshot=snapshot,
                        membership_window=membership_window)
            quote['source'] = SOURCE_ID
            if symbol in self._factors:
                quote['raw']['qfq_factors'] = [{'date': str(current), 'factor': str(factor)}
                                              for current, factor in self._factors[symbol]]
            return quote
        except (SinaRateLimitError, MarketDependencyError):
            raise
        except (SinaDataError, KeyError, ValueError, TypeError) as error:
            code = getattr(error, 'code', 'quote_unavailable')
            if code == 'adjustment_unavailable':
                # Incomplete factors are failed evidence, not a successful cache.
                self._factors.pop(symbol, None)
            if code in ('paused_unknown', 'snapshot_invalid', 'snapshot_quote_mismatch'):
                invalidate = getattr(self.archives, 'invalidate', None)
                if callable(invalidate):
                    invalidate(current)
                self._daily.pop(current, None)
            return result | {'error': code}
