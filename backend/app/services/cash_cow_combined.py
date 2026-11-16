"""Cash-cow sources: explicit provenance, no adjusted prices or inferred cash totals."""
from datetime import date, timedelta
import time
import re
import logging

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
        self.check_cancel()
        time.sleep(max(0, 2 - (time.monotonic() - self.last_request)))
        LOG.info('Cash cow request provider=%s endpoint=%s symbol=%s period=%s', provider, endpoint, params.get('symbol') or params.get('ts_code') or '-', params.get('period') or '-')
        try:
            if provider == 'asharehub':
                response = self.client.get('https://asharehub.com/v2/' + endpoint,
                    params=params, headers={'X-API-Key': self.ash_key}, follow_redirects=False)
            else:
                response = self.client.post('https://api.tushare.pro', json={
                    'api_name': endpoint, 'token': self.ts_token, 'params': params,
                    'fields': ('cal_date,is_open' if endpoint == 'trade_cal' else 'ts_code,trade_date,close')}, follow_redirects=False)
            if response.status_code == 429:
                raise CashCowSourceError(f'{provider} 请求受限，请等待配额恢复后重试。')
            if response.status_code in (401, 403):
                raise CashCowSourceError(f'{provider} 凭据无效或接口无权限，请检查系统配置。')
            response.raise_for_status()
            if len(response.content) > 24 * 1024 * 1024:
                raise ValueError('response_too_large')
            return response.json()
        except CashCowSourceError:
            raise
        except Exception as exc:
            LOG.warning('Cash cow source failure provider=%s endpoint=%s type=%s', provider, endpoint, type(exc).__name__)
            # Never attach HTTP exception/request objects containing credentials.
            raise CashCowSourceError(f'{provider} {endpoint} 数据读取失败，未发布本次结果。') from None
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
        verified = {f"{row['code']}.{row['exchange']}" for row in census}
        if len(verified) != len(census) or verified != set(self.symbols.values()):
            current = set(self.symbols.values())
            LOG.warning('Cash cow universe mismatch asharehub=%s selector=%s asharehub_only=%s selector_only=%s', len(current), len(verified), sorted(current-verified)[:10], sorted(verified-current)[:10])
            raise CashCowSourceError('AShareHub 与独立股票池覆盖不一致，未发布部分结果。')
        # Compare date provenance before applying age: do not mistake a code
        # migration or an earlier OTC admission for an exchange listing date.
        census_dates = {row['code']: row.get('list_date') for row in census}
        for stock in stocks:
            if census_dates[stock['code']] != stock['list_date']:
                LOG.warning('Cash cow listing date unverified symbol=%s asharehub_date=%s selector_date=%s',
                            self.symbols[stock['code']], stock['list_date'], census_dates[stock['code']])
                raise CashCowSourceError('上市日期跨来源不一致，无法核验上市年限，保留原结果。')
        return stocks

    def financials(self, year, codes=None):
        # Eastmoney remains authoritative for absolute adjusted profit. Read it
        # in bulk; do not substitute ROE or growth rates for adjusted earnings.
        result = super().financials(year, codes)
        for endpoint, field, target in [('financials/income', 'n_income_attr_p', 'net_profit'),
                                        ('financials/cash-flow', 'n_cashflow_act', 'operating_cashflow')]:
            rows = self._ash(endpoint, {'period': f'{year}1231', 'report_type': '1'})
            wanted = set(codes or result)
            selected, versions = {}, {}
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
            # The fully paginated EM report remains the authoritative annual
            # source. AS is a cross-check, not proof that every issuer must have
            # a report in this period. Missing EM fields retain original
            # 'insufficient' semantics; never invent zeros or infer no dividend.
            missing = wanted - set(selected)
            if missing:
                LOG.info('Cash cow financial crosscheck unavailable endpoint=%s year=%s count=%s sample=%s authoritative=eastmoney',
                         endpoint, year, len(missing), sorted(missing)[:10])
            for code in wanted & set(selected) & set(result):
                expected = result[code][target]
                if expected is None:
                    continue
                if abs(selected[code] - expected) > max(.01, abs(expected) * 1e-8):
                    raise CashCowSourceError('财报跨来源数值不一致，保留原结果。')
                result[code][target] = selected[code]
        return result

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
