"""Cash-cow sources: explicit provenance, no adjusted prices or inferred cash totals."""
from datetime import date, timedelta
import time
import re
import logging
import httpx

LOG = logging.getLogger(__name__)

from app.services.system_integrations import integrations_config
from app.services.cash_cow_source import EastmoneyCashCowSource, CashCowSourceError, number


class CombinedCashCowSource(EastmoneyCashCowSource):
    def __init__(self, client, *, settings, **kwargs):
        super().__init__(client, **kwargs)
        integrations = integrations_config(settings)
        self.ash_key = integrations['asharehub']['token']
        self.ts_token = integrations['tushare']['token']
        if not self.ash_key or not self.ts_token:
            raise CashCowSourceError('请先在系统配置中填写 AShareHub 和 Tushare 凭据。')
        self.symbols = {}
        self.listed_dates = {}
        self.calendars = {}
        self.unavailable_calendars = set()

    def _request(self, provider, endpoint, params):
        attempts = 3 if provider == 'tushare' else 1
        for attempt in range(1, attempts + 1):
            self.check_cancel()
            # Include a quiet interval after failed attempts, not just successes.
            delay = max(2, 2 ** (attempt - 1))
            time.sleep(max(0, delay - (time.monotonic() - self.last_request)))
            self.check_cancel()
            started, response, stage = time.monotonic(), None, 'request'
            symbol = params.get('symbol') or params.get('ts_code') or '-'
            LOG.info('Cash cow request provider=%s endpoint=%s symbol=%s period=%s start=%s end=%s attempt=%s/%s',
                     provider, endpoint, symbol, params.get('period') or '-',
                     params.get('start_date') or '-', params.get('end_date') or '-', attempt, attempts)
            try:
                if provider == 'asharehub':
                    response = self.client.get('https://asharehub.com/v2/' + endpoint,
                        params=params, headers={'X-API-Key': self.ash_key}, follow_redirects=False)
                else:
                    response = self.client.post('https://api.tushare.pro', json={
                        'api_name': endpoint, 'token': self.ts_token, 'params': params,
                        'fields': ('cal_date,is_open' if endpoint == 'trade_cal' else 'ts_code,trade_date,close')},
                        timeout=httpx.Timeout(60, connect=10), follow_redirects=False)
                stage = 'http_status'
                response.raise_for_status()
                stage = 'decode'
                if len(response.content) > 24 * 1024 * 1024:
                    raise ValueError('response_too_large')
                result = response.json()
                if provider == 'tushare' and isinstance(result, dict) and result.get('code') != 0:
                    api_code = result.get('code')
                    LOG.warning('Cash cow API rejection provider=%s endpoint=%s symbol=%s api_code=%s',
                                provider, endpoint, symbol, api_code if type(api_code) is int else '-')
                LOG.info('Cash cow response provider=%s endpoint=%s symbol=%s attempt=%s http_status=%s '
                         'received_bytes=%s elapsed_seconds=%.3f', provider, endpoint, symbol, attempt,
                         response.status_code, len(response.content), time.monotonic() - started)
                return result
            except Exception as exc:
                status = response.status_code if response is not None else None
                transient = (isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
                             or isinstance(exc, httpx.HTTPStatusError) and status is not None and 500 <= status <= 599)
                retry = transient and attempt < attempts
                # No exception strings, URLs, request objects or provider bodies:
                # they can contain the configured credential or echo it back.
                LOG.warning('Cash cow source failure provider=%s endpoint=%s symbol=%s start=%s end=%s '
                            'attempt=%s/%s stage=%s type=%s http_status=%s elapsed_seconds=%.3f retry=%s',
                            provider, endpoint, symbol, params.get('start_date') or '-', params.get('end_date') or '-',
                            attempt, attempts, stage, type(exc).__name__, status if status is not None else '-',
                            time.monotonic() - started, retry)
                if retry:
                    continue
                if status == 429:
                    message = f'{provider} 请求受限，请等待配额恢复后重试。'
                elif status in (401, 403):
                    message = f'{provider} 凭据无效或接口无权限，请检查系统配置。'
                elif isinstance(exc, httpx.TimeoutException):
                    message = f'{provider} {endpoint} 请求超时，未发布本次结果。'
                elif isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
                    message = f'{provider} {endpoint} 连接中断，未发布本次结果。'
                elif stage == 'decode':
                    message = f'{provider} {endpoint} 返回格式异常，未发布本次结果。'
                elif status is not None:
                    message = f'{provider} {endpoint} HTTP {status}，未发布本次结果。'
                else:
                    message = f'{provider} {endpoint} 数据读取失败，未发布本次结果。'
                raise CashCowSourceError(message) from None
            finally:
                self.last_request = time.monotonic()

    def _ash(self, endpoint, params):
        # Do not reuse pre-validation list caches. An earlier truncated payload
        # must be repairable by the next request, not poison the whole day.
        rows = self._request('asharehub', endpoint, params)
        if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
            raise CashCowSourceError('AShareHub 数据为空或格式异常。')
        return rows

    def universe(self):
        rows = self._ash('reference/stocks', {'list_status': 'L'})
        stocks, seen, exchanges = [], set(), set()
        for row in rows:
            symbol = row.get('symbol', '')
            if not isinstance(symbol, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', symbol):
                raise CashCowSourceError('AShareHub 股票代码无效。')
            code, exchange = symbol.split('.')
            listed = row.get('list_date', '')
            try:
                listed_day = date.fromisoformat(f'{listed[:4]}-{listed[4:6]}-{listed[6:]}')
            except (ValueError, TypeError):
                raise CashCowSourceError('AShareHub 上市日期无效。') from None
            if (code in seen or row.get('list_status') != 'L' or not row.get('name')
                    or listed_day.isoformat() > self.as_of):
                raise CashCowSourceError('AShareHub 股票池不完整或存在重复。')
            seen.add(code); exchanges.add(exchange); self.symbols[code] = symbol
            self.listed_dates[code] = listed_day.isoformat()
            stocks.append({'code': code, 'name': row['name'], 'exchange': exchange, 'list_date': listed_day.isoformat()})
        if not {'SH', 'SZ', 'BJ'} <= exchanges:
            raise CashCowSourceError('AShareHub 股票池缺少市场。')
        # The selector has a complete-page/nextpage contract and does not use
        # Tushare stock_basic's restrictive trial quota. Never reuse clist data.
        census = self._selector_universe()
        from app.services.cash_cow_rules import listed_three_years
        verified = {f"{row['code']}.{row['exchange']}": row for row in census}
        current = {f"{row['code']}.{row['exchange']}": row for row in stocks}
        if len(verified) != len(census):
            raise CashCowSourceError('独立股票池存在重复，未发布部分结果。')
        # Reconcile the full raw pools before eligibility. Shared-code date
        # conflicts must not be hidden by filtering either provider first.
        for symbol in current.keys() & verified.keys():
            if current[symbol]['list_date'] != verified[symbol].get('list_date'):
                LOG.warning('Cash cow listing date unverified symbol=%s asharehub_date=%s selector_date=%s',
                            symbol, current[symbol]['list_date'], verified[symbol].get('list_date'))
                raise CashCowSourceError('上市日期跨来源不一致，无法核验上市年限，保留原结果。')
        missing = []
        for provider, pool, other in [('asharehub', current, verified), ('eastmoney_selector', verified, current)]:
            for symbol in sorted(pool.keys() - other.keys()):
                listed = pool[symbol].get('list_date')
                try:
                    parsed = date.fromisoformat(listed)
                except (TypeError, ValueError):
                    raise CashCowSourceError('股票池上市日期缺失或无效，无法核验覆盖范围。') from None
                if parsed > date.fromisoformat(self.as_of) or listed_three_years(listed, self.as_of):
                    missing.append(symbol)
                else:
                    LOG.info('Cash cow universe ineligible difference source=%s symbol=%s listed=%s as_of=%s reason=listed_under_three_years',
                             provider, symbol, listed, self.as_of)
        if missing:
            LOG.warning('Cash cow universe mismatch asharehub=%s selector=%s asharehub_only=%s selector_only=%s eligible_unverified=%s',
                        len(current), len(verified), sorted(current.keys()-verified.keys())[:10],
                        sorted(verified.keys()-current.keys())[:10], sorted(missing)[:10])
            raise CashCowSourceError('AShareHub 与独立股票池覆盖不一致，未发布部分结果。')
        # EM supplies the fully paginated current universe. All age-eligible
        # members and their listing dates have been independently reconciled.
        # EM-only young listings remain in exclusion totals, never fetched.
        self.symbols = {row['code']: symbol for symbol, row in verified.items()}
        self.listed_dates = {row['code']: row['list_date'] for row in census}
        LOG.info('Cash cow universe reconciled asharehub=%s selector=%s eligible=%s as_of=%s',
                 len(current), len(census), sum(listed_three_years(row['list_date'], self.as_of) for row in census), self.as_of)
        return census

    def financials(self, year, codes=None):
        # All three monetary fields use the same CNY annual EM statement.
        # AS report_type=1 is an original statement, while EM can incorporate
        # subsequently restated comparatives. Equality is not a validity gate:
        # a mismatch is evidence of a discrepancy, not proof of bad data or of
        # a restatement. Never mix one provider's profit with another's CFO.
        result = super().financials(year, codes)
        for endpoint, field, target in [('financials/income', 'n_income_attr_p', 'net_profit'),
                                        ('financials/cash-flow', 'n_cashflow_act', 'operating_cashflow')]:
            try:
                self._financial_crosscheck(year, codes, result, endpoint, field, target)
            except CashCowSourceError:
                self.check_cancel()
                # Cross-check availability cannot veto a validated EM report.
                # Do not log exception text or request objects with credentials.
                LOG.warning('Cash cow financial crosscheck unavailable endpoint=%s year=%s '
                            'authoritative=eastmoney policy=single_source_annual comparison_basis=unverified',
                            endpoint, year)
        return result

    def _financial_crosscheck(self, year, codes, result, endpoint, field, target):
        rows = self._ash(endpoint, {'period': f'{year}1231', 'report_type': '1'})
        wanted = set(result if codes is None else codes)
        selected, versions, metadata = {}, {}, {}
        for row in rows:
            symbol = row.get('symbol', '')
            code = symbol.split('.')[0] if isinstance(symbol, str) else ''
            if code not in wanted:
                continue
            if (row.get('end_date') != f'{year}1231' or symbol != self.symbols.get(code)
                    or row.get('report_type') != '1'
                    or number(row.get(field)) is None):
                raise CashCowSourceError('AShareHub 年报字段缺失、重复或口径不一致。')
            announced = row.get('f_ann_date') or row.get('ann_date')
            if not isinstance(announced, str) or not re.fullmatch(r'\d{8}', announced):
                raise CashCowSourceError('财报公告日期无效。')
            if announced > self.as_of.replace('-', ''):
                continue
            if code in versions and announced < versions[code]:
                continue
            value = number(row[field])
            if versions.get(code) == announced and selected[code] != value:
                raise CashCowSourceError('同日财报版本冲突。')
            versions[code] = announced
            selected[code] = value
            metadata[code] = {
                key: row[key] if isinstance(row.get(key), str) and re.fullmatch(r'\d{8}', row[key]) else '-'
                for key in ('ann_date', 'f_ann_date')
            }
        missing = wanted - set(selected)
        if missing:
            LOG.info('Cash cow financial crosscheck unavailable endpoint=%s year=%s count=%s sample=%s authoritative=eastmoney',
                     endpoint, year, len(missing), sorted(missing)[:10])
        mismatches, compared = 0, 0
        for code in sorted(wanted & set(selected) & set(result)):
            expected = result[code][target]
            if expected is None:
                continue
            compared += 1
            difference = selected[code] - expected
            tolerance = max(.01, abs(expected) * 1e-8)
            if abs(difference) > tolerance:
                mismatches += 1
                if mismatches <= 20:
                    em_dates = {}
                    for key in ('NOTICE_DATE', 'UPDATE_DATE'):
                        value = self.financial_metadata.get(year, {}).get(code, {}).get(key)
                        em_dates[key] = value[:10] if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}(?: 00:00:00)?', value) else '-'
                    LOG.warning(
                        'Cash cow financial mismatch symbol=%s year=%s endpoint=%s field=%s '
                        'asharehub_value=%.17g eastmoney_value=%.17g difference=%.17g tolerance=%.17g '
                        'asharehub_report_type=1 asharehub_ann_date=%s asharehub_f_ann_date=%s '
                        'eastmoney_notice_date=%s eastmoney_update_date=%s eastmoney_report_version=unavailable authoritative=eastmoney '
                        'comparison_basis=unverified action=use_authoritative_annual',
                        self.symbols[code], year, endpoint, target, selected[code], expected,
                        difference, tolerance, metadata[code]['ann_date'], metadata[code]['f_ann_date'],
                        em_dates['NOTICE_DATE'], em_dates['UPDATE_DATE'])
        LOG.info('Cash cow financial crosscheck summary endpoint=%s year=%s compared=%s mismatches=%s '
                 'detail_limit=20 authoritative=eastmoney policy=single_source_annual',
                 endpoint, year, compared, mismatches)

    def _calendar(self, start_year, end_year, exchange):
        requested_start, requested_end = start_year, end_year
        start_year = min(start_year, int(self.as_of[:4]) - 10)
        end_year = max(end_year, int(self.as_of[:4]) - 1)
        # AShareHub documents SSE/SZSE only: never infer a BJ calendar.
        calendar_exchange = {'SH': 'SSE', 'SZ': 'SZSE'}.get(exchange)
        if calendar_exchange is None:
            raise CashCowSourceError('北交所交易日历尚无法独立核验，保留原结果。')
        key = (start_year, end_year, calendar_exchange)
        if key in self.calendars:
            return {d for d in self.calendars[key] if requested_start <= int(d[:4]) <= requested_end}
        params = {'exchange': calendar_exchange,
                  'start_date': f'{start_year}0101', 'end_date': f'{end_year}1231'}
        payload = self._cached('ash_tradecal_verified_v1', [params])
        if payload is None:
            payload = self._ash('reference/trade-calendar', params)
        if not isinstance(payload, list) or not payload:
            raise CashCowSourceError('AShareHub 交易日历无法完整核验，保留原结果。')
        first, last = date(start_year, 1, 1), date(end_year, 12, 31)
        expected = {(first + timedelta(days=n)).strftime('%Y%m%d') for n in range((last-first).days+1)}
        found, sessions = set(), set()
        for row in payload:
            if (not isinstance(row, dict) or row.get('exchange') != calendar_exchange
                    or row.get('cal_date') not in expected or row['cal_date'] in found
                    or type(row.get('is_open')) is not int or row['is_open'] not in (0, 1)):
                raise CashCowSourceError('交易日历重复、缺失或格式异常。')
            found.add(row['cal_date'])
            if row['is_open']:
                sessions.add(row['cal_date'])
        if found != expected or not sessions:
            raise CashCowSourceError('交易日历不完整，保留原结果。')
        self.calendars[key] = sessions
        self._save_cache('ash_tradecal_verified_v1', [params], payload)
        return {d for d in sessions if requested_start <= int(d[:4]) <= requested_end}

    def prices(self, code, start_year, end_year):
        symbol = self.symbols[code]
        params = {'ts_code': symbol, 'start_date': f'{start_year}0101',
                  'end_date': self.as_of.replace('-', '')}
        payload = self._cached('tushare_raw_v1', [params])
        if payload is None:
            payload = self._request('tushare', 'daily', params)
        if not isinstance(payload, dict) or payload.get('code') != 0:
            message = str(payload.get('msg', '')) if isinstance(payload, dict) else ''
            if '频率' in message or '次数' in message:
                raise CashCowSourceError('Tushare 日线调用频率受限，请等待配额恢复后重试。')
            raise CashCowSourceError('Tushare 日线无权限或读取失败，请检查接口权限。')
        data = payload.get('data') or {}
        fields, items = data.get('fields'), data.get('items')
        if (fields != ['ts_code', 'trade_date', 'close'] or not isinstance(items, list)
                or not items or len(items) >= 6000):
            raise CashCowSourceError('Tushare 日线为空、格式异常或可能被截断。')
        daily, seen = [], set()
        for row in items:
            if not isinstance(row, list) or len(row) != 3:
                raise CashCowSourceError('Tushare 日线结构异常。')
            sym, day, close = row
            try:
                parsed = date.fromisoformat(f'{day[:4]}-{day[4:6]}-{day[6:]}').isoformat()
            except (ValueError, TypeError):
                raise CashCowSourceError('Tushare 日线日期异常。') from None
            if (sym != symbol or day in seen or not params['start_date'] <= day <= params['end_date']
                    or number(close) is None or number(close) <= 0):
                raise CashCowSourceError('Tushare 日线重复、越界或价格无效。')
            seen.add(day); daily.append((parsed, number(close)))
        daily.sort()
        # Calendar coverage is an audit, not a requirement that suspended
        # stocks have a row on every open day. Never label absent rows suspended.
        missing = None
        exchange = symbol.split('.')[1]
        try:
            if exchange in self.unavailable_calendars:
                raise CashCowSourceError('calendar audit unavailable in this run')
            sessions = self._calendar(start_year, end_year, exchange)
        except CashCowSourceError:
            self.unavailable_calendars.add(exchange)
            sessions = None
            LOG.warning('Cash cow price calendar audit unavailable code=%s coverage=unverified policy=returned_trading_days', code)
        if sessions is not None:
            listed = self.listed_dates.get(code, f'{start_year}-01-01').replace('-', '')
            expected = {day for day in sessions if day >= listed}
            observed = {d.replace('-', '') for d, _ in daily if start_year <= int(d[:4]) <= end_year}
            if observed - expected:
                raise CashCowSourceError('行情日期与交易日历不一致。')
            missing = expected - observed
            if missing:
                LOG.warning('Cash cow price gaps code=%s missing=%s sample=%s suspension_status=unverified policy=returned_trading_days',
                            code, len(missing), sorted(missing)[:10])
        self._save_cache('tushare_raw_v1', [params], payload)
        self.daily[code] = daily
        result = {}
        for year in range(start_year, end_year + 1):
            bars = [(d, c) for d, c in daily if d.startswith(str(year))]
            if not bars:
                raise CashCowSourceError(f'{code} {year} 年无有效成交行情，保留原结果。')
            result[year] = dict(year_average_close=sum(c for _, c in bars)/len(bars),
                                price_date=bars[-1][0], trading_days=len(bars),
                                price_basis='returned_unadjusted_trading_days',
                                missing_open_days=None if missing is None else sum(d.startswith(str(year)) for d in missing),
                                suspension_verified=False)
            LOG.info('Cash cow annual prices code=%s year=%s rows=%s first=%s last=%s basis=returned_unadjusted_trading_days missing_open_days=%s suspension_verified=False',
                     code, year, len(bars), bars[0][0], bars[-1][0], result[year]['missing_open_days'])
        return result
