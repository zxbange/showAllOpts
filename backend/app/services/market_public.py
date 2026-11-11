"""Dated public-market inputs for the original JoinQuant calculation.

The exchange master defines membership, never a code-prefix allowlist. Public
TDX daily archives supply prices and turnover. Corporate-action records select
windows requiring Sina adjustment factors. Missing bars require affirmative
full-session suspension evidence; they are never inferred from absence.
"""
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
import logging

from app.services.growth_dividend_source import fetch_calendar
from app.services.market_methodology import JOINQUANT_HEAT_METHOD
from app.services.market_history import audit_historical_membership, equity_namespace
from app.services.market_sina import (
    SinaDataError, SinaMarketProvider, SinaRateLimitError, _bars, _day, _number,
    _symbol, calculate_joinquant_quote,
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
                 actions=None, suspensions=None, calendar_loader=None, factor_url=FACTOR_URL, tencent_qfq_url=TENCENT_QFQ_URL, counts=None):
        super().__init__(client)
        if master is None:
            from app.services.market_universe import ExchangeSecurityMaster
            master = ExchangeSecurityMaster(self.client)
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
        if wanted != self._wanted_symbols:
            # One run may visit several point-in-time pools. A filtered memory
            # cache cannot silently hide a since-delisted member in an older pool.
            self._daily.clear()
            self._raw_equities.clear()
            self._archive_evidence.clear()
        self._wanted_symbols = wanted

    def _sessions(self, day):
        if self._calendar_days is None:
            days = [_day(value) for value in self.calendar_loader(self.client)]
            if not days or days != sorted(set(days)):
                raise SinaDataError('交易日历不完整。', code='calendar_unavailable')
            self._calendar_days = days
        try:
            index = self._calendar_days.index(day)
        except ValueError:
            raise SinaDataError('目标日期不是已确认交易日。', code='calendar_incomplete') from None
        if index < 19:
            raise SinaDataError('缺少均线所需的交易日历。', code='calendar_incomplete')
        return self._calendar_days[index - 19:index + 1]

    def _daily_rows(self, day):
        if day not in self._daily:
            data = self.archives.fetch(day)
            if data.get('date') != str(day) or data.get('verified') is not True:
                raise SinaDataError('每日行情包日期或完整性未通过校验。', code='snapshot_invalid')
            rows = data['rows']
            rows = list(rows.values()) if isinstance(rows, dict) else rows
            by_symbol = {row['symbol']: row for row in rows}
            if len(by_symbol) != len(rows) or any(row.get('date') != str(day) for row in rows):
                raise SinaDataError('每日行情包股票或日期不完整。', code='snapshot_invalid')
            self._archive_evidence[day] = {'date': str(day), 'sha256': data.get('source_digest'),
                                           'source_url': data.get('source_url'), 'parsed_records': len(by_symbol)}
            self._raw_equities[day] = {symbol: row for symbol, row in by_symbol.items()
                                       if equity_namespace(symbol) or (self._wanted_symbols is not None and symbol in self._wanted_symbols)}
            LOG.info('Market source archive date=%s source=tdx records=%d selected=%d',
                     day, len(by_symbol), len(set(by_symbol) & self._wanted_symbols) if self._wanted_symbols is not None else len(by_symbol))
            self._daily[day] = {symbol: row for symbol, row in by_symbol.items()
                                if self._wanted_symbols is None or symbol in self._wanted_symbols}
        return self._daily[day]

    def _pauses(self, start, end):
        key = (start, end)
        if key not in self._pause_data:
            self._pause_data[key] = self.suspensions.fetch(start, end)
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
        except SinaRateLimitError:
            raise
        except SinaDataError as error:
            if error.code == 'unsupported_historical_schema':
                raise
            raise SinaDataError('目标日交易所分类统计未通过校验。', code='historical_count_unavailable') from error

    def fetch_universe(self, day):
        try:
            return self._fetch_universe(day)
        except (SinaDataError, ValueError):
            invalidate = getattr(self.archives, 'invalidate', None)
            if callable(invalidate):
                invalidate(_day(day))
            self._daily.pop(_day(day), None)
            raise

    def _fetch_universe(self, day):
        day = _day(day)
        sessions = self._sessions(day)
        master = self.master.fetch(day)
        LOG.info('Market source master date=%s source=exchange securities=%d historical_verified=%s',
                 day, len(master.get('rows', [])), master.get('evidence', {}).get('membership_verified') is True)
        if (master.get('date') != str(day) or not master.get('rows')
                or master.get('expected_count') != len(master['rows'])):
            raise SinaDataError('交易所证券主表不完整。', code='universe_incomplete')
        historical_audit = master.get('evidence', {}).get('membership_verified') is not True
        if historical_audit and any(master.get('evidence', {}).get(key) is not True for key in
                                    ('source_counts_verified', 'listing_dates_verified', 'current_catalogue_verified')):
            raise SinaDataError('历史主表来源和上市区间证据不完整。', code='universe_coverage_unverified')
        self.prepare_snapshot(master)
        current = self._daily_rows(day)
        rows = []
        for member in master['rows']:
            symbol = _symbol(member['symbol'])
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
            universe_evidence = audit_historical_membership(
                master | {'source_archive_evidence': self._archive_evidence[day]},
                rows, self._raw_equities[day], self._pauses(sessions[0], day), self._historical_counts(day))
            LOG.info('Market historical audit date=%s traded=%d paused=%d total=%d', day,
                     universe_evidence['traded_identity_count'], universe_evidence['paused_identity_count'], len(rows))
        result = {'date': str(day), 'expected_count': len(rows), 'rows': rows,
                  'source': SOURCE_ID, 'scope': 'sse_szse_stock_and_cdr',
                  'source_has_trading_date': True, 'dated_snapshot_verified': True,
                  'date_verified_by': 'public_daily_archive',
                  'universe_evidence': universe_evidence,
                  'fetched_at': datetime.now(timezone.utc).isoformat()}
        for row in rows:
            row['universe_evidence'] = {key: universe_evidence.get(key) for key in
                ('membership_basis', 'target_date', 'historical_reconstruction_verified', 'joinquant_universe_directly_verified')}
        self._contexts[day] = result
        return result

    def _window_actions(self, symbol, sessions):
        if self._action_data is None:
            self._action_data = self.actions.fetch()
        data = self._action_data
        if data.get('verified') is not True:
            raise SinaDataError('除权资料不完整。', code='adjustment_unavailable')
        if _day(data['coverage_through']) < sessions[-1]:
            raise SinaDataError('除权资料尚未更新到目标日。', code='adjustment_unavailable')
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
            listed_from, listed_until = self._listing(snapshot['listing'], day)
            relevant = [value for value in sessions if listed_from <= value <= listed_until]
            evidence = {'calendar_verified': True, 'listing_verified': True,
                        'paused_verified': True, 'prices_verified': True, 'universe_verified': True,
                        'price_basis': 'qfq', 'adjustment_basis': 'excluded without MA20',
                        'sources': ['exchange_master', 'tdx_daily', 'public_suspension_intervals'],
                        'universe_basis': snapshot.get('universe_evidence', {}).get('membership_basis', 'verified_snapshot')}
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
                        listed_from=listed_from, listed_until=listed_until, evidence=evidence, snapshot=snapshot)
            quote['source'] = SOURCE_ID
            if symbol in self._factors:
                quote['raw']['qfq_factors'] = [{'date': str(current), 'factor': str(factor)}
                                              for current, factor in self._factors[symbol]]
            return quote
        except SinaRateLimitError:
            raise
        except (SinaDataError, KeyError, ValueError, TypeError) as error:
            code = getattr(error, 'code', 'quote_unavailable')
            if code in ('paused_unknown', 'snapshot_invalid', 'snapshot_quote_mismatch'):
                invalidate = getattr(self.archives, 'invalidate', None)
                if callable(invalidate):
                    invalidate(current)
                self._daily.pop(current, None)
            return result | {'error': code}
