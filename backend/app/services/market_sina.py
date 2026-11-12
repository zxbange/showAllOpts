"""Structured Sina observations and the original JoinQuant calculation contract.

The live source has verified dated quotes and qfq factors, but does not yet have
complete historical suspension/listing evidence. It therefore cannot publish
JoinQuant-method heat. Legacy unadjusted API MA20 remains explicitly unverified.
No HTML is parsed. HTTP 403/429 stop collection without retries or host changes.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
import math
import re
from typing import Any

import httpx
import pandas as pd

from app.core.diagnostics import diagnostic_details
from app.services.market_methodology import JOINQUANT_HEAT_METHOD, LEGACY_SINA_HEAT_METHOD


LOG = logging.getLogger(__name__)

UNIVERSE_URL = (
    'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
    'Market_Center.getHQNodeData'
)
COUNT_URL = (
    'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
    'Market_Center.getHQNodeStockCount'
)
KLINE_URL = (
    'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData'
)
HQ_URL = 'https://hq.sinajs.cn/?list='
HQ_BATCH_SIZE = 40  # Sina's own datas.hq SDK splits quote requests at 40 symbols.
PAGE_SIZE = 80
SOURCE = '新浪财经'
ADJUSTMENT = 'unadjusted'
_SYMBOL = re.compile(r'(?:sh|sz|bj)\d{6}\Z')
_NUMBER = re.compile(r'[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')


class SinaDataError(ValueError):
    """The response cannot safely represent the requested market observations."""

    def __init__(self, message, *, code='invalid_response', status_code=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class MarketDependencyError(SinaDataError):
    """A shared input/connection failed; stop this execution before fan-out."""

    def __init__(self, code):
        super().__init__('公共采集依赖暂不可用。', code=code)


class SinaRateLimitError(SinaDataError):
    """Access denied or rate limited: stop this batch without automatic retries."""

    def __init__(self, status_code, *, retry_after=None):
        super().__init__('新浪接口限制访问，请暂停采集。',
                         code='rate_limited', status_code=status_code)
        self.retry_after = retry_after
        self.is_rate_limited = True


def _day(value):
    if isinstance(value, datetime):
        raise SinaDataError('必须提供交易日期，不能使用带时间的日期。', code='invalid_date')
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise SinaDataError('交易日期格式无效。', code='invalid_date')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SinaDataError('交易日期无效。', code='invalid_date') from None


def _number(value, *, positive=False, integer=False):
    if isinstance(value, bool) or value is None:
        raise SinaDataError('行情数值缺失或无效。', code='invalid_number')
    text = str(value).strip()
    if len(text) > 80 or not _NUMBER.fullmatch(text):
        raise SinaDataError('行情数值格式无效。', code='invalid_number')
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise SinaDataError('行情数值格式无效。', code='invalid_number') from None
    # Bound the exponent before rendering a canonical decimal string.
    if (not result.is_finite() or result < 0 or (positive and result == 0)
            or abs(result.as_tuple().exponent) > 30 or len(result.as_tuple().digits) > 40
            or (integer and result != result.to_integral_value())):
        raise SinaDataError('行情数值不在有效范围。', code='invalid_number')
    return result


def _decimal_text(value, **kwargs):
    return format(_number(value, **kwargs), 'f')


def _symbol(value):
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        raise SinaDataError('股票代码无效。', code='invalid_symbol')
    return value


def _parse_json(text):
    """Parse JSON or the observed =(JSON); wrapper, without evaluating script."""
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    # Sina prepends a script-looking block comment to some JSONP responses.
    # It remains inert text; only the JSON assignment below is accepted.
    while text.startswith('/*'):
        end = text.find('*/')
        if end < 0:
            raise SinaDataError('新浪行情响应不是有效 JSON。', code='invalid_json')
        text = text[end + 2:].strip()
    match = re.fullmatch(
        r'(?:(?:var\s+)?[A-Za-z_$][\w$]*\s*)?=\s*\((.*)\)\s*;?',
        text, flags=re.DOTALL,
    )
    if match:
        try:
            return json.loads(match.group(1))
        except (ValueError, TypeError):
            pass
    raise SinaDataError('新浪行情响应不是有效 JSON。', code='invalid_json')


def _count(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not re.fullmatch(r'\d+', str(value))):
        raise SinaDataError('新浪股票总数无效。', code='invalid_count')
    count = int(value)
    if not 0 < count <= 100000:
        raise SinaDataError('新浪股票总数无效。', code='invalid_count')
    return count


def _bars(payload):
    if not isinstance(payload, list) or not payload:
        raise SinaDataError('新浪未返回日线记录。', code='empty_bars')
    result = []
    seen = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise SinaDataError('新浪日线记录结构无效。', code='invalid_bar')
        day = _day(raw.get('day'))
        if day in seen:
            raise SinaDataError('新浪日线出现重复日期。', code='duplicate_date')
        seen.add(day)
        close = _decimal_text(raw.get('close'), positive=True)
        volume = _decimal_text(raw.get('volume'), integer=True)
        ma = raw.get('ma_price20')
        # Missing and invalid MA are different. An invalid zero/NaN must never
        # become a valid MA, nor be taken as evidence of a newly listed stock.
        ma20 = None if ma in (None, '') else _decimal_text(ma, positive=True)
        result.append({'date': str(day), 'close': close, 'ma20': ma20,
                       'volume': volume, 'raw': raw})
    return sorted(result, key=lambda row: row['date'])


def _snapshot_matches(snapshot, symbol, bar):
    if snapshot is None:
        return True
    return (
        snapshot.get('symbol') == symbol
        and _number(snapshot.get('close')) == _number(bar['close'], positive=True)
        and _number(snapshot.get('volume'), integer=True) == _number(bar['volume'], integer=True)
    )


def _hq_row(symbol, fields, day):
    if not isinstance(fields, list) or any(not isinstance(value, str) for value in fields):
        raise SinaDataError('新浪实时行情字段无效。', code='invalid_hq')
    if len(fields) < 33 or _day(fields[30]) != day:
        raise SinaDataError('新浪实时行情日期不符或缺失。', code='hq_date_mismatch')
    if not fields[0] or not re.fullmatch(r'\d{2}:\d{2}:\d{2}', fields[31]):
        raise SinaDataError('新浪实时行情字段无效。', code='invalid_hq')
    volume = _number(fields[8], integer=True)
    amount = _number(fields[9])
    state = fields[32]
    # Official CN parser: state 02/03 means stopped for the whole day.
    # A real 2026-11-10 paused stock (688053) nevertheless returned 00, so
    # 00 + zero volume is UNKNOWN, never proof that paused is zero or one.
    paused = (1 if volume == 0 and amount == 0 else None) if state in ('02', '03') else (
        0 if volume > 0 else None)
    return {
        'symbol': symbol, 'name': fields[0], 'date': str(day), 'time': fields[31],
        'close': _decimal_text(fields[3]), 'prev_close': _decimal_text(fields[2]),
        'volume': format(volume, 'f'), 'amount': format(amount, 'f'),
        'hq_state': state, 'paused': paused, 'hq_raw': fields,
    }


def _hq_matches_node(row, quote):
    if (_number(row['close']) != _number(quote['close'])
            or _number(row['volume'], integer=True) != _number(quote['volume'], integer=True)
            or _number(row['amount']) != _number(quote['amount']).quantize(
                Decimal('1'), rounding=ROUND_HALF_UP)):
        raise SinaDataError('当日股票快照与实时行情价量不一致。', code='snapshot_quote_mismatch')
    return {**deepcopy(row), 'node_amount': row['amount'], **deepcopy(quote)}


def _hq_rows(text, symbols, day):
    """Parse quoted data assignments only; never execute the returned script."""
    pattern = re.compile(r'var\s+hq_str_((?:sh|sz|bj)\d{6})\s*=\s*'
                         r'("(?:[^"\\]|\\.)*")\s*;')
    result, position = {}, 0
    for match in pattern.finditer(text):
        if text[position:match.start()].strip():
            raise SinaDataError('新浪实时行情结构无效。', code='invalid_hq')
        position = match.end()
        symbol = match.group(1)
        if symbol not in symbols or symbol in result:
            raise SinaDataError('新浪实时行情股票代码重复或不符。', code='hq_symbol_mismatch')
        try:
            fields = json.loads(match.group(2)).split(',')
        except ValueError:
            raise SinaDataError('新浪实时行情字段无效。', code='invalid_hq') from None
        result[symbol] = _hq_row(symbol, fields, day)
    if text[position:].strip() or set(result) != set(symbols):
        raise SinaDataError('新浪实时行情未返回完整股票集合。', code='incomplete_hq')
    return result


class SinaMarketProvider:
    """Synchronous adapter; an injected client can pace/cancel each HTTP request.

    Normalized numbers are decimal strings, so both source fields and raw HTTP
    responses remain JSON serializable without losing comparison precision.
    The caller owns injected clients; only an internally created client closes.
    """

    def __init__(self, client=None, *, timeout=20.0):
        self._owns_client = client is None
        self.client = client if client is not None else httpx.Client(
            timeout=timeout, follow_redirects=False,
        )
        self.timeout = timeout
        self._reported_http_failures = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_client:
            self.client.close()

    @staticmethod
    def heat_capability():
        return {
            'methodology': JOINQUANT_HEAT_METHOD, 'verified': False,
            'price_basis': 'qfq', 'denominator': 'eligible',
            'error': 'heat_methodology_unverified',
            'missing_evidence': ['historical_paused', 'listing_dates', 'qfq_ma20_window'],
        }

    def _request_text(self, url, params, raw_requests, *, headers=None):
        kwargs = {'timeout': self.timeout, 'follow_redirects': False}
        if params is not None:
            kwargs['params'] = params
        if headers is not None:
            kwargs['headers'] = headers
        try:
            response = self.client.get(url, **kwargs)
        except httpx.HTTPError as error:
            failure = type(error).__name__
            if failure not in self._reported_http_failures:
                self._reported_http_failures.add(failure)
                LOG.warning('Market source request failed stage=get_text source=market_http %s', diagnostic_details(error))
            if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
                raise MarketDependencyError('source_unavailable') from None
            raise SinaDataError('新浪行情网络请求失败。', code='network_error') from None
        raw_requests.append({'url': url, 'params': dict(params) if params is not None else None,
                             'status_code': response.status_code, 'text': response.text})
        if response.status_code in (403, 429):
            retry_after = response.headers.get('Retry-After')
            if retry_after is not None:
                try:
                    retry_after = float(_number(retry_after))
                except SinaDataError:
                    retry_after = None
            raise SinaRateLimitError(response.status_code, retry_after=retry_after)
        if not 200 <= response.status_code < 300:
            raise SinaDataError('新浪行情 HTTP 请求未成功。', code='http_error',
                                status_code=response.status_code)
        return response.text

    def _request(self, url, params, raw_requests):
        return _parse_json(self._request_text(url, params, raw_requests))

    def fetch_universe(self, day):
        """Capture the complete current hs_a list, associated with requested day.

        The API cannot request a historical universe. Historical retries must
        reuse the snapshot saved that day, not call this method for a past date.
        Per-stock daily bars verify date/close/volume later; ticktime is not a date.
        """
        day = _day(day)
        requests = []
        before_raw = self._request(COUNT_URL, {'node': 'hs_a'}, requests)
        count = _count(before_raw)
        rows, pages, seen = [], [], set()
        for page in range(1, (count + PAGE_SIZE - 1) // PAGE_SIZE + 1):
            payload = self._request(UNIVERSE_URL, {
                'node': 'hs_a', 'num': str(PAGE_SIZE), 'page': str(page),
                'sort': 'symbol', 'asc': '1', 'symbol': '', '_s_r_a': 'page',
            }, requests)
            expected = min(PAGE_SIZE, count - (page - 1) * PAGE_SIZE)
            if not isinstance(payload, list) or len(payload) != expected:
                raise SinaDataError('新浪股票名单分页不完整。', code='incomplete_page')
            pages.append(payload)
            for raw in payload:
                if not isinstance(raw, dict):
                    raise SinaDataError('新浪股票名单记录无效。', code='invalid_stock')
                symbol = _symbol(raw.get('symbol'))
                if symbol in seen:
                    raise SinaDataError('新浪股票名单出现重复代码。', code='duplicate_symbol')
                if raw.get('code') not in (None, symbol[2:]):
                    raise SinaDataError('新浪股票代码不一致。', code='symbol_mismatch')
                if not isinstance(raw.get('name'), str) or not raw['name'].strip():
                    raise SinaDataError('新浪股票名称缺失。', code='invalid_stock')
                seen.add(symbol)
                rows.append({
                    'symbol': symbol, 'name': raw['name'],
                    'close': _decimal_text(raw.get('trade')),
                    'volume': _decimal_text(raw.get('volume'), integer=True),
                    'amount': _decimal_text(raw.get('amount')),
                    'ticktime': raw.get('ticktime'), 'raw': raw,
                })
        after_raw = self._request(COUNT_URL, {'node': 'hs_a'}, requests)
        if _count(after_raw) != count or len(rows) != count:
            raise SinaDataError('采集期间股票总数变化，名单需要重新获取。', code='count_changed')
        if {row['symbol'][:2] for row in rows} != {'sh', 'sz', 'bj'}:
            raise SinaDataError('新浪股票名单缺少沪深北市场。', code='missing_market')
        return {
            'date': str(day), 'expected_count': count, 'rows': rows,
            'source': SOURCE, 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'source_has_trading_date': False,
            'raw': {'count_before': before_raw, 'pages': pages, 'count_after': after_raw,
                    'requests': requests},
        }

    def fetch_dated_snapshot(self, universe, day, *, cached_rows=None, checkpoint=None):
        """Bind every universe member to dated HQ quotes and exact yuan amounts.

        The node endpoint rounds amounts to yuan. Require exact agreement with
        the rounded HQ amount, and with close/volume, before adopting HQ's more
        precise amount. All batches must pass; partial captures are not returned.
        The worker may persist each verified batch through checkpoint, bound to
        the day and original node snapshot hash, and pass it back as cached_rows.
        Cached raw fields are parsed and matched again before skipping a request.
        """
        day = _day(day)
        if not isinstance(universe, dict) or universe.get('date') != str(day):
            raise SinaDataError('股票快照目标日期不一致。', code='snapshot_date_mismatch')
        rows = _universe_rows(universe)
        result = deepcopy(universe)
        raw_requests, received = [], {}
        by_symbol = {row['symbol']: row for row in rows}
        if cached_rows is not None:
            cache = list(cached_rows.values()) if isinstance(cached_rows, dict) else cached_rows
            if not isinstance(cache, (list, tuple)):
                raise SinaDataError('实时行情缓存结构无效。', code='invalid_hq_cache')
            for cached in cache:
                symbol = _symbol(cached.get('symbol')) if isinstance(cached, dict) else None
                if symbol not in by_symbol or symbol in received:
                    raise SinaDataError('实时行情缓存代码不符或重复。', code='invalid_hq_cache')
                quote = _hq_row(symbol, cached.get('hq_raw'), day)
                received[symbol] = _hq_matches_node(by_symbol[symbol], quote)
        symbols = [row['symbol'] for row in rows if row['symbol'] not in received]
        for offset in range(0, len(symbols), HQ_BATCH_SIZE):
            batch = symbols[offset:offset + HQ_BATCH_SIZE]
            # The official API treats %2C as part of a symbol, so concatenate
            # validated symbols using literal comma separators, as its SDK does.
            text = self._request_text(HQ_URL + ','.join(batch), None, raw_requests,
                                      headers={'Referer': 'https://finance.sina.com.cn/'})
            parsed = _hq_rows(text, batch, day)
            verified = [_hq_matches_node(by_symbol[symbol], parsed[symbol]) for symbol in batch]
            received.update({row['symbol']: row for row in verified})
            if checkpoint is not None:
                checkpoint(deepcopy(verified))
        result['rows'] = [received[row['symbol']] for row in rows]
        result['source_has_trading_date'] = True
        result['dated_snapshot_verified'] = True
        result['date_verified_by'] = 'sina_hq'
        result.setdefault('raw', {})['hq_requests'] = raw_requests
        return result

    def latest_session_day(self):
        """Latest actual index bar, a readiness signal rather than a holiday rule."""
        payload = self._request(KLINE_URL, {
            'symbol': 'sh000001', 'scale': '240', 'ma': 'no', 'datalen': '1',
        }, [])
        if not isinstance(payload, list) or not payload:
            raise SinaDataError('新浪未返回指数交易日。', code='empty_bars')
        days = []
        for raw in payload:
            if not isinstance(raw, dict):
                raise SinaDataError('新浪指数日线结构无效。', code='invalid_bar')
            _number(raw.get('close'), positive=True)
            days.append(_day(raw.get('day')))
        if len(set(days)) != len(days):
            raise SinaDataError('新浪指数日线出现重复日期。', code='duplicate_date')
        return max(days)

    def fetch_quote(self, symbol, day, *, snapshot=None, datalen=1):
        """Return an exact-date MA20 observation or an explicit unresolved reason.

        For a missing MA, request at least 20 bars. Only a response shorter than
        its requested window proves the beginning of available history was
        reached. Count observations through the target date, excluding later
        bars. One bounded expansion can disambiguate a historical target near
        a full window's start; no replacement average is ever calculated.
        """
        symbol, day = _symbol(symbol), _day(day)
        if isinstance(datalen, bool) or not isinstance(datalen, int) or not 1 <= datalen <= 1970:
            raise SinaDataError('日线条数必须介于 1 与 1970。', code='invalid_datalen')
        result: dict[str, Any] = {
            'symbol': symbol, 'day': str(day), 'date': None, 'close': None,
            'ma20': None, 'volume': None, 'status': 'transient_failure', 'error': None,
            'history_count': None, 'history_complete': False,
            'history_requested': None, 'history_returned': None,
            'source': SOURCE, 'adjustment': ADJUSTMENT,
            'methodology': LEGACY_SINA_HEAT_METHOD, 'verified': False,
            'price_basis': 'bfq', 'denominator': 'universe',
            'raw': {'requests': []},
        }

        def request_bars(length):
            payload = self._request(KLINE_URL, {
                'symbol': symbol, 'scale': '240', 'ma': '20', 'datalen': str(length),
            }, result['raw']['requests'])
            return _bars(payload)

        try:
            bars = request_bars(datalen)
            selected = next((bar for bar in bars if bar['date'] == str(day)), None)
            if selected is None:
                past = [bar for bar in bars if bar['date'] < str(day)]
                if not past:
                    result['error'] = 'requested_day_missing'
                    return result
                selected = past[-1]
                result.update({key: selected[key] for key in ('date', 'close', 'ma20', 'volume')})
                result['status'] = 'stale'
                result['error'] = 'requested_day_missing'
                if (snapshot is not None and snapshot.get('symbol') == symbol
                        and _number(snapshot.get('volume'), integer=True) == 0
                        and _number(snapshot.get('amount')) == 0):
                    result['status'], result['error'] = 'no_trade', None
                return result

            result.update({key: selected[key] for key in ('date', 'close', 'ma20', 'volume')})
            if (not _snapshot_matches(snapshot, symbol, selected)
                    or _number(selected['volume'], integer=True) == 0):
                result['status'], result['error'] = 'mismatch', 'snapshot_quote_mismatch'
                return result
            if selected['ma20'] is not None:
                result['status'] = 'current'
                return result
            requested = datalen
            # The ordinary path makes one request. Missing MA requires at least
            # a 20-record window; historical targets in a full, truncated window
            # may get one further expansion, always bounded by the API's limit.
            requests_left = 2 if requested < 20 else 1
            while True:
                through_target = sum(bar['date'] <= str(day) for bar in bars)
                reached_start = requested >= 20 and len(bars) < requested
                result.update(history_count=through_target, history_complete=reached_start,
                              history_requested=requested, history_returned=len(bars))
                if requested >= 20 and through_target < 20 and reached_start:
                    result['status'] = 'insufficient_history'
                    return result
                if (requested >= 20 and (through_target >= 20 or requested == 1970)
                        or requests_left == 0):
                    result['status'], result['error'] = 'unknown_missing_ma', 'ma20_missing'
                    return result
                requested = 20 if requested < 20 else min(
                    1970, max(requested * 2, requested + 20 - through_target))
                requests_left -= 1
                bars = request_bars(requested)
                selected = next((bar for bar in bars if bar['date'] == str(day)), None)
                if selected is None:
                    result['error'] = 'history_confirmation_missing_day'
                    result['history_count'] = None
                    result['history_complete'] = False
                    return result
                result.update(
                    history_count=sum(bar['date'] <= str(day) for bar in bars),
                    history_complete=len(bars) < requested,
                    history_requested=requested, history_returned=len(bars),
                )
                result.update({key: selected[key] for key in ('date', 'close', 'ma20', 'volume')})
                if (not _snapshot_matches(snapshot, symbol, selected)
                        or _number(selected['volume'], integer=True) == 0):
                    result['status'], result['error'] = 'mismatch', 'snapshot_quote_mismatch'
                    return result
                if selected['ma20'] is not None:
                    result['status'] = 'current'
                    return result
        except (SinaRateLimitError, MarketDependencyError):
            raise
        except SinaDataError as exc:
            result['status'], result['error'] = 'transient_failure', exc.code
            result['history_count'], result['history_complete'] = None, False
            return result


def _universe_rows(universe):
    rows = universe.get('rows') if isinstance(universe, dict) else universe
    if not isinstance(rows, (list, tuple)) or not rows:
        raise SinaDataError('股票名单为空。', code='empty_universe')
    symbols = [_symbol(row.get('symbol')) for row in rows if isinstance(row, dict)]
    if len(symbols) != len(rows) or len(set(symbols)) != len(rows):
        raise SinaDataError('股票名单代码缺失或重复。', code='duplicate_symbol')
    if isinstance(universe, dict) and universe.get('expected_count', len(rows)) != len(rows):
        raise SinaDataError('股票名单总数不匹配。', code='incomplete_universe')
    return rows


_HEAT_EVIDENCE = ('calendar_verified', 'listing_verified', 'paused_verified',
                  'prices_verified', 'universe_verified')


def _require_heat_evidence(evidence):
    """Validate a caller's explicit, independently established source contract.

    These flags are assertions about the input evidence, not a means to turn
    unknown Sina fields into verified data. The live adapter cannot supply them.
    """
    if (not isinstance(evidence, dict)
            or any(evidence.get(key) is not True for key in _HEAT_EVIDENCE if key != 'listing_verified')
            or not (evidence.get('listing_verified') is True or
                    (evidence.get('dated_membership_verified') is True
                     and isinstance(evidence.get('membership_window'), dict)))
            or evidence.get('price_basis') != 'qfq'
            or not isinstance(evidence.get('adjustment_basis'), str)
            or not evidence['adjustment_basis'].strip()):
        raise SinaDataError('聚宽口径所需的基础数据证据不完整。', code='heat_methodology_unverified')


def _above_joinquant(close, mean):
    close, mean = float(_number(close, positive=True)), float(_number(mean, positive=True))
    if not math.isfinite(close) or not math.isfinite(mean):
        raise SinaDataError('复权价格或均线无效。', code='invalid_number')
    return (close - mean) > max(abs(close), abs(mean)) * 1e-12


def verified_membership_sessions(symbol, day, sessions, window):
    """Independent 20-day audit mask, never IPO/relisting inferred from absence."""
    from app.services.market_history import DATED_SOURCE, DATED_EVIDENCE_VERSION
    if (not isinstance(window, dict) or window.get('symbol') != symbol
            or window.get('source_id') != DATED_SOURCE
            or type(window.get('evidence_version')) is not int or window['evidence_version'] != DATED_EVIDENCE_VERSION
            or window.get('target_date') != str(day)):
        raise SinaDataError('逐日证券范围证据不完整。', code='dated_membership_window_unverified')
    records = window.get('sessions')
    if (not isinstance(records, list) or len(records) != 20
            or [item.get('date') if isinstance(item, dict) else None for item in records] != [str(d) for d in sessions]
            or any(item.get('audited') is not True or type(item.get('present')) is not bool
                   or not re.fullmatch(r'[0-9a-f]{64}', str(item.get('report_digest', '')))
                   or not re.fullmatch(r'[0-9a-f]{64}', str(item.get('audit_digest', ''))) for item in records)):
        raise SinaDataError('完整二十日证券范围尚未核实。', code='dated_membership_window_unverified')
    present = [item['present'] for item in records]
    if all(present):
        if window.get('ipo_evidence') is not None:
            raise SinaDataError('完整窗口不应混入其他上市范围证明。', code='dated_membership_window_unverified')
        return set(sessions)
    if not present[-1] or True not in present:
        raise SinaDataError('目标日证券身份缺失。', code='dated_membership_gap')
    first = present.index(True)
    if not all(present[first:]):
        raise SinaDataError('证券存在中间缺口或重新进入。', code='dated_membership_gap')
    from app.services.market_listing import validate_listing_evidence
    ipo = window.get('ipo_evidence')
    try:
        validate_listing_evidence(ipo, symbol, day)
    except ValueError:
        raise SinaDataError('上市日期原始证明不完整或身份冲突。', code='dated_membership_gap') from None
    if ipo.get('ipo_date') != str(sessions[first]):
        raise SinaDataError('显式上市日期与逐日证券范围不吻合。', code='dated_membership_gap')
    return set(sessions[first:])


def calculate_joinquant_quote(symbol, day, *, calendar, rows, listed_from=None,
                             listed_until=None, evidence, snapshot, membership_window=None):
    """Apply the original script to one stock's verified 20-session window.

    This pure function makes no API calls. The caller must already have verified
    the calendar, target universe, listing interval, one adjustment basis, and
    explicit paused=0/1 for every listed session. A missing price can be filled
    only on an explicitly paused session with a known prior adjusted close.
    The first window price must be supplied even if that session was paused.
    No rounding is applied before pandas' original 20-session rolling mean.

    Each input row has date, close_qfq and paused. The target row also carries
    raw_close and volume to bind it to the independently captured snapshot.
    """
    symbol, day = _symbol(symbol), _day(day)
    _require_heat_evidence(evidence)
    if not isinstance(calendar, (list, tuple)):
        raise SinaDataError('必须提供完整的 20 个市场交易日。', code='invalid_calendar')
    sessions = [_day(value) for value in calendar]
    if (len(sessions) != 20 or sessions != sorted(set(sessions)) or sessions[-1] != day):
        raise SinaDataError('必须提供截至目标日期的连续 20 个市场交易日。', code='invalid_calendar')
    start = end = None
    if membership_window is not None:
        if listed_from is not None or listed_until is not None or evidence.get('dated_membership_verified') is not True:
            raise SinaDataError('逐日证券证据与上市区间不可混用。', code='dated_membership_window_unverified')
        expected = verified_membership_sessions(symbol, day, sessions, membership_window)
        if evidence.get('membership_window') != membership_window:
            raise SinaDataError('逐日证券证据来源不一致。', code='dated_membership_window_unverified')
    else:
        start, end = _day(listed_from), _day(listed_until)
        if not start <= day <= end:
            raise SinaDataError('目标日期不在已验证的上市区间内。', code='listing_date_mismatch')
        expected = {current for current in sessions if start <= current <= end}
    if not isinstance(rows, (list, tuple)):
        raise SinaDataError('复权日线结构无效。', code='invalid_observations')
    by_day = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SinaDataError('复权日线结构无效。', code='invalid_observation')
        current = _day(row.get('date'))
        if current in by_day or current not in sessions or current not in expected:
            raise SinaDataError('复权日线日期重复或超出有效区间。', code='invalid_observation_date')
        by_day[current] = row
    if set(by_day) != expected:
        raise SinaDataError('已上市交易日的停牌或复权行情缺失。', code='incomplete_heat_window')
    values, previous, filled = [], None, []
    for current in sessions:
        if current not in expected:
            values.append(float('nan'))
            continue
        row = by_day[current]
        paused = row.get('paused')
        if isinstance(paused, bool) or not isinstance(paused, int) or paused not in (0, 1):
            raise SinaDataError('历史停牌状态未经确认。', code='paused_unknown')
        close = row.get('close_qfq')
        if close is None and paused == 1 and previous is not None:
            close = previous
            filled.append(str(current))
        adjusted = _number(close, positive=True)
        if paused == 1 and previous is not None and adjusted != previous:
            raise SinaDataError('停牌日复权价格与前收盘价不一致。', code='paused_price_mismatch')
        value = float(adjusted)
        if not math.isfinite(value):
            raise SinaDataError('复权价格无效。', code='invalid_number')
        values.append(value)
        previous = adjusted
    current = by_day[day]
    raw_close = _decimal_text(current.get('raw_close'), positive=True)
    volume = _decimal_text(current.get('volume'), integer=True)
    if (not isinstance(snapshot, dict)
            or snapshot.get('date', str(day)) != str(day)
            or not _snapshot_matches(snapshot, symbol, {'close': raw_close, 'volume': volume})
            or (current['paused'] == 1 and _number(volume) != 0)):
        raise SinaDataError('复权观察与目标日原始行情不一致。', code='snapshot_quote_mismatch')
    mean = float(pd.Series(values, dtype=float).rolling(window=20, min_periods=20).mean().iloc[-1])
    enough = math.isfinite(mean)
    eligible = enough and current['paused'] == 0
    above = eligible and _above_joinquant(values[-1], mean)
    status = 'paused' if current['paused'] == 1 else ('current' if enough else 'insufficient_history')
    return {
        'symbol': symbol, 'day': str(day), 'date': str(day), 'close': raw_close,
        'volume': volume, 'qfq_close': str(values[-1]),
        'ma20': str(mean) if enough else None, 'ma20_qfq': str(mean) if enough else None,
        'paused': current['paused'], 'history_count': len(expected),
        'status': status, 'eligible': eligible, 'above': bool(above), 'error': None,
        'methodology': JOINQUANT_HEAT_METHOD, 'verified': True,
        'price_basis': 'qfq', 'denominator': 'eligible', 'adjustment': 'qfq',
        'source': SOURCE, 'evidence': deepcopy(evidence),
        'raw': {'calendar': [str(value) for value in sessions],
                'listing': {'start': str(start), 'end': str(end)} if start is not None else None,
                'membership_window': deepcopy(membership_window),
                'rows': deepcopy(rows), 'filled_paused_dates': filled},
    }


def aggregate_heat(universe, observations):
    """Publish only verified qfq observations using the original eligible divisor.

    Legacy API MA20, zero-volume guesses, missing history and unverified pauses
    are unresolved. Explicit current pauses and confirmed IPO exclusions resolve
    a stock but do not enter the divisor. Zero eligible stocks cannot publish.
    """
    rows = _universe_rows(universe)
    target = _day(universe['date']) if isinstance(universe, dict) and 'date' in universe else None
    by_symbol, duplicates = {}, set()
    observations = list(observations.values()) if isinstance(observations, dict) else observations
    if not isinstance(observations, (list, tuple)):
        raise SinaDataError('日线记录集合无效。', code='invalid_observations')
    for observation in observations:
        if not isinstance(observation, dict) or not isinstance(observation.get('symbol'), str):
            raise SinaDataError('日线记录缺少股票代码。', code='invalid_observation')
        symbol = observation['symbol']
        if symbol in by_symbol:
            duplicates.add(symbol)
        by_symbol[symbol] = observation
    above = valid = insufficient = paused_count = 0
    pending = []
    for row in rows:
        symbol = row['symbol']
        observation = by_symbol.get(symbol)
        if observation is None or symbol in duplicates:
            pending.append(symbol)
            continue
        try:
            if (observation.get('methodology') != JOINQUANT_HEAT_METHOD
                    or observation.get('verified') is not True
                    or observation.get('price_basis') != 'qfq'
                    or observation.get('denominator') != 'eligible'):
                raise SinaDataError('均线记录不是已验证的聚宽口径。')
            _require_heat_evidence(observation.get('evidence'))
            expected_day = target or _day(observation.get('day'))
            membership_expected = None
            if observation['evidence'].get('dated_membership_verified') is True:
                window = observation['evidence']['membership_window']
                sessions = [_day(item['date']) for item in window.get('sessions', [])]
                if len(sessions) != 20 or sessions != sorted(set(sessions)) or sessions[-1] != expected_day:
                    raise SinaDataError('逐日证券窗口日期无效。', code='dated_membership_window_unverified')
                membership_expected = verified_membership_sessions(symbol, expected_day, sessions, window)
                bound = row.get('universe_evidence', {})
                if (bound.get('source_id') != window['source_id']
                        or bound.get('audit_digest') != window['sessions'][-1]['audit_digest']
                        or bound.get('report_digest') != window['sessions'][-1]['report_digest']):
                    raise SinaDataError('热度窗口与目标日股票池来源不一致。', code='dated_membership_window_unverified')
            if (_day(observation.get('day')) != expected_day
                    or _day(observation.get('date')) != expected_day
                    or not _snapshot_matches(row, symbol, observation)):
                raise SinaDataError('日线请求日期不一致。')
            status = observation.get('status')
            count = observation.get('history_count')
            paused = observation.get('paused')
            if membership_expected is not None and count != len(membership_expected):
                raise SinaDataError('历史长度与逐日上市证明不一致。', code='dated_membership_gap')
            if (isinstance(count, bool) or not isinstance(count, int) or not 0 < count <= 20
                    or isinstance(paused, bool) or not isinstance(paused, int) or paused not in (0, 1)):
                raise SinaDataError('交易日窗口或停牌状态不完整。')
            _number(observation.get('qfq_close'), positive=True)
            if status == 'paused':
                if (paused != 1 or observation.get('eligible') is not False
                        or observation.get('above') is not False or _number(row.get('volume')) != 0):
                    raise SinaDataError('当日停牌记录未经确认。')
                paused_count += 1
            elif status == 'current':
                if paused != 0 or count != 20 or observation.get('eligible') is not True:
                    raise SinaDataError('有效均线记录不完整。')
                is_above = _above_joinquant(observation.get('qfq_close'), observation.get('ma20'))
                if observation.get('above') is not is_above:
                    raise SinaDataError('均线比较结果不一致。')
                above += int(is_above)
                valid += 1
            elif status == 'insufficient_history':
                if (paused != 0 or count >= 20 or observation.get('ma20') is not None
                        or observation.get('eligible') is not False or observation.get('above') is not False):
                    raise SinaDataError('上市不足 20 个交易日尚未确认。')
                insufficient += 1
            else:
                pending.append(symbol)
        except (SinaDataError, KeyError):
            pending.append(symbol)
    unexpected = sorted(set(by_symbol) - {row['symbol'] for row in rows})
    publishable = not pending and not unexpected and valid > 0
    ratio = float((Decimal(above) / valid).quantize(Decimal('0.0000000001'))) if publishable else None
    quality = {'valid_count': valid, 'insufficient_history_count': insufficient,
               'paused_count': paused_count, 'no_trade_count': paused_count, 'pending_count': len(pending)}
    return {
        'above_count': above, 'universe_count': len(rows), 'valid_count': valid,
        'eligible_count': valid, 'insufficient_history_count': insufficient,
        'paused_count': paused_count, 'no_trade_count': paused_count, 'pending_count': len(pending),
        'pending_symbols': pending, 'unexpected_symbols': unexpected,
        'publishable': publishable, 'ratio': ratio, 'value': ratio,
        'data_quality': quality, 'source': SOURCE, 'adjustment': 'qfq',
        'methodology': JOINQUANT_HEAT_METHOD, 'price_basis': 'qfq', 'denominator': 'eligible',
        'error': None if publishable else ('heat_observations_incomplete' if pending or unexpected
                                           else 'zero_eligible_count'),
    }


def aggregate_crowding(universe):
    """Top floor(N*5%) amount / whole-universe amount, including zero rows.

    Preserve the original script's minimum 100-stock gate and output precision.
    A zero market-wide total is unresolved, never a zero ratio.
    """
    rows = _universe_rows(universe)
    amounts = [(row['symbol'], _number(row.get('amount'))) for row in rows]
    amounts.sort(key=lambda item: (-item[1], item[0]))
    count = len(rows)
    top_count = max(count // 20, 1)
    total = sum((value for _, value in amounts), Decimal(0))
    top = sum((value for _, value in amounts[:top_count]), Decimal(0))
    publishable = count >= 100 and total > 0
    ratio = float((top / total).quantize(Decimal('0.0000000001'))) if publishable else None
    total_text, top_text = str(total.quantize(Decimal('0.01'))), str(top.quantize(Decimal('0.01')))
    return {
        'stock_count': count, 'universe_count': count,
        'top_stock_count': top_count, 'top_count': top_count,
        'total_money': total_text, 'total_amount': total_text,
        'top_money': top_text, 'top_amount': top_text,
        'top_symbols': [symbol for symbol, _ in amounts[:top_count]],
        'ratio': ratio, 'value': ratio, 'publishable': publishable,
        'error': (None if publishable else 'zero_total_amount' if total == 0 else 'insufficient_universe'),
        'source': SOURCE,
    }
