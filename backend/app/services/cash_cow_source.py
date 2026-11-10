"""Read-only Eastmoney adapter. Amounts CNY; percentages in percentage points.

Free public website endpoints are not contractual APIs. Missing or inconsistent
responses fail closed. No credential, local-file or paid-service dependency.
"""
from datetime import date, timedelta
from contextlib import nullcontext
import json
import math
import re
import time

import httpx
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

DATACENTER = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
UNIVERSE = 'https://82.push2.eastmoney.com/api/qt/clist/get'
PRICES = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
MAX_BYTES = 12 * 1024 * 1024
MAX_REDIRECTS = 3
# Only known public API origins, never arbitrary eastmoney.com subdomains.
# The quote service redirects some server networks to its delayed-data host.
REDIRECT_HOSTS = {
    '82.push2.eastmoney.com': {'82.push2.eastmoney.com', '82.push2delay.eastmoney.com'},
    'push2his.eastmoney.com': {'push2his.eastmoney.com', 'push2hisdelay.eastmoney.com'},
    'datacenter-web.eastmoney.com': {'datacenter-web.eastmoney.com'},
    'datacenter.eastmoney.com': {'datacenter.eastmoney.com'},
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class CashCowSourceError(RuntimeError):
    pass


def cash_rate(text):
    # Only a cash distribution, optionally followed by a tax description.
    match = re.fullmatch(r'\s*10派([0-9]+(?:\.[0-9]+)?)元(?:[（(]含税[^()（）]*[)）])?\s*', text)
    return float(match.group(1)) / 10 if match else None


def corporate_action(text):
    if text in ('不分配不转增', '不分配', '不转增'):
        return False
    return bool(re.search(r'送[0-9]|转[0-9]|配股|拆股|合股', text))


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class EastmoneyCashCowSource:
    def __init__(self, client, check_cancel=lambda: None, progress=None, as_of=None, cache_dir=None):
        self.client = client
        self.check_cancel = check_cancel
        self.progress = progress
        self.as_of = str(as_of or date.today())[:10]
        self.secids = {}
        self.events = {}
        self.daily = {}
        self.capital = {}
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.last_request = 0.0

    def _cache_path(self, kind, parameters):
        if self.cache_dir is None:
            return None
        key = json.dumps([2, self.as_of, kind, parameters], sort_keys=True, ensure_ascii=True)
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def _cached(self, kind, parameters):
        self.check_cancel()
        path = self._cache_path(kind, parameters)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAX_BYTES:
                return None
            with path.open('rb') as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            wrapper = json.loads(raw)
            if wrapper.get('as_of') != self.as_of or wrapper.get('complete') is not True:
                return None
            if kind == 'daily' or (kind == 'rows' and parameters[0] == 'RPT_F10_DIVIDEND_MAIN'):
                saved = number(wrapper.get('saved_at'))
                if saved is None or not 0 <= time.time() - saved <= 300:
                    return None
            return wrapper.get('data')
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _save_cache(self, kind, parameters, data):
        self.check_cancel()
        path = self._cache_path(kind, parameters)
        if path is None:
            return
        encoded = json.dumps(dict(as_of=self.as_of, complete=True, saved_at=time.time(), data=data),
                             ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_BYTES:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pending-', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.check_cancel()
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _rate_limit(self):
        remaining = .2 - (time.monotonic() - self.last_request)
        while remaining > 0:
            self.check_cancel()
            time.sleep(min(.05, remaining))
            remaining = .2 - (time.monotonic() - self.last_request)
        self.check_cancel()
        self.last_request = time.monotonic()

    def _get(self, url, params):
        # At most two total attempts; never retry auth denials or bad JSON.
        for attempt in range(2):
            try:
                return self._get_once(url, params)
            except CashCowSourceError as exc:
                cause = exc.__cause__
                retryable = isinstance(cause, httpx.TimeoutException) or (
                    isinstance(cause, httpx.HTTPStatusError)
                    and (cause.response.status_code == 429 or cause.response.status_code >= 500))
                if attempt or not retryable:
                    raise
                for _ in range(5):
                    self.check_cancel()
                    time.sleep(.1)
        raise CashCowSourceError('数据源重试失败')

    def _get_once(self, url, params):
        try:
            original = httpx.URL(url, params=params)
            current, visited = original, {str(original)}
            for redirects in range(MAX_REDIRECTS + 1):
                # Bound/cancel every hop and override an injected client's own
                # automatic redirect setting before any destination is visited.
                self._rate_limit()
                streaming = hasattr(self.client, 'stream')
                context = (self.client.stream('GET', current, timeout=25, follow_redirects=False)
                           if streaming else nullcontext(self.client.get(
                               current, timeout=25, follow_redirects=False)))
                with context as response:
                    if response.status_code in REDIRECT_STATUSES:
                        if redirects >= MAX_REDIRECTS:
                            raise CashCowSourceError('东方财富数据源重定向次数过多，已停止采集')
                        current = self._redirect_target(original, current, response.headers.get('location'))
                        if str(current) in visited:
                            raise CashCowSourceError('东方财富数据源重定向循环，已停止采集')
                        visited.add(str(current))
                        # Close without downloading the redirect response body.
                        continue
                    response.raise_for_status()
                    # Streaming bounds decompressed bytes as well as JSON size,
                    # including the final response after a permitted redirect.
                    chunks, size = [], 0
                    for chunk in response.iter_bytes() if streaming else [response.content]:
                        self.check_cancel()
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise CashCowSourceError('数据源响应过大，已停止采集')
                        chunks.append(chunk)
                    data = json.loads(b''.join(chunks))
                    break
        except CashCowSourceError:
            raise
        except Exception as exc:
            # Cancellation raised by caller must not be turned into a retry/error.
            self.check_cancel()
            raise CashCowSourceError('东方财富数据读取失败，请稍后重试') from exc
        if not isinstance(data, dict):
            raise CashCowSourceError('数据源响应格式异常')
        return data

    @staticmethod
    def _redirect_target(original, current, location):
        if not location:
            raise CashCowSourceError('东方财富数据源重定向地址缺失')
        try:
            target = current.join(location)
            allowed = REDIRECT_HOSTS.get(original.host, set())
            # Same API path and complete query: a redirect must not silently
            # remove the market scope, report year, page number or price basis.
            valid = (target.scheme == 'https' and target.host in allowed
                     and target.port in (None, 443)
                     and urlsplit(location).username is None
                     and urlsplit(str(target)).username is None
                     and not target.fragment
                     and target.raw_path.split(b'?')[0] == original.raw_path.split(b'?')[0]
                     and sorted(target.params.multi_items()) == sorted(original.params.multi_items()))
        except (ValueError, httpx.InvalidURL):
            valid = False
        if not valid:
            raise CashCowSourceError('东方财富数据源重定向地址不受信任或请求参数改变，已停止采集')
        return target

    def _rows(self, report, filter_text, key, sort='SECURITY_CODE', columns='ALL'):
        parameters = [report, filter_text, sort, columns]
        cached = self._cached('rows', parameters)
        if isinstance(cached, list) and all(isinstance(r, dict) for r in cached):
            keys = [key(r) for r in cached]
            expected = re.search(r"REPORT_DATE='(\d{4}-12-31)'", filter_text)
            if (all(keys) and len(set(keys)) == len(keys) and
                    (not expected or all(str(r.get('REPORT_DATE') or '')[:10] == expected.group(1) for r in cached))):
                return cached
        rows = self._rows_uncached(report, filter_text, key, sort, columns)
        expected = re.search(r"REPORT_DATE='(\d{4}-12-31)'", filter_text)
        if expected and any(str(r.get('REPORT_DATE') or '')[:10] != expected.group(1) for r in rows):
            raise CashCowSourceError('年度报表日期缺失或与请求年份不符')
        self._save_cache('rows', parameters, rows)
        return rows

    def _rows_uncached(self, report, filter_text, key, sort='SECURITY_CODE', columns='ALL'):
        rows, seen, total, pages = [], set(), None, None
        page = 1
        while True:
            endpoint = ('https://datacenter.eastmoney.com/securities/api/data/v1/get'
                        if report in ('RPT_F10_DIVIDEND_MAIN', 'RPT_F10_EH_EQUITY') else DATACENTER)
            data = self._get(endpoint, dict(reportName=report, columns=columns,
                filter=filter_text, pageNumber=page, pageSize=500,
                sortColumns=sort, sortTypes='1', source='WEB', client='WEB'))
            result = data.get('result')
            # Live-verified no-record response, only valid before any pages.
            if (page == 1 and data.get('success') is False and result is None
                    and data.get('code') == 9201 and data.get('message') == '返回数据为空'):
                return []
            if data.get('success') is not True or not isinstance(result, dict):
                raise CashCowSourceError('数据源报表不可用或未返回完整结果')
            count, page_count, batch = result.get('count'), result.get('pages'), result.get('data')
            if (type(count) is not int or type(page_count) is not int or count < 0
                    or not 0 <= page_count <= 1000 or not isinstance(batch, list)):
                raise CashCowSourceError('数据源分页元数据异常')
            expected_pages = (count + 499) // 500
            if page_count != expected_pages and not (count == 0 and page_count == 1):
                raise CashCowSourceError('数据源分页总数不一致')
            if total is None:
                total, pages = count, page_count
            if (total, pages) != (count, page_count):
                raise CashCowSourceError('采集期间数据总数变化，请重新采集')
            if len(batch) != min(500, max(0, count - (page - 1) * 500)):
                raise CashCowSourceError('数据源分页不完整')
            for row in batch:
                if not isinstance(row, dict):
                    raise CashCowSourceError('数据源记录格式异常')
                identity = key(row)
                if not identity or identity in seen:
                    raise CashCowSourceError('数据源存在重复或缺少标识的记录')
                seen.add(identity)
                rows.append(row)
            if page >= max(1, pages):
                break
            page += 1
            if self.progress:
                self.progress(f'正在读取数据源第 {page}/{pages} 页')
        if len(rows) != total:
            raise CashCowSourceError('数据源记录数量校验失败')
        return rows

    def universe(self):
        cached = self._cached('universe', [])
        if isinstance(cached, list) and cached and all(isinstance(r, dict) and
                re.fullmatch(r'\d{6}', str(r.get('code', ''))) and r.get('exchange') in ('SH', 'SZ', 'BJ')
                and isinstance(r.get('name'), str) for r in cached):
            if len({r['code'] for r in cached}) == len(cached):
                self.secids.update({r['code']: f"{1 if r['exchange'] == 'SH' else 0}.{r['code']}" for r in cached})
                return cached
        stocks = self._universe_uncached()
        self._save_cache('universe', [], stocks)
        return stocks

    def _universe_uncached(self):
        stocks, seen, total = [], set(), None
        page = 1
        while True:
            payload = self._get(UNIVERSE, dict(pn=page, pz=100, po=1, np=1,
                fltt=2, invt=2, fid='f12',
                fs='m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048',
                fields='f12,f13,f14,f26'))
            data = payload.get('data')
            if payload.get('rc') != 0 or not isinstance(data, dict):
                raise CashCowSourceError('沪深京股票列表不可用')
            count, batch = data.get('total'), data.get('diff')
            if type(count) is not int or not 1 <= count <= 20000 or not isinstance(batch, list):
                raise CashCowSourceError('股票列表分页格式异常')
            if total is None:
                total = count
            if total != count or len(batch) != min(100, total - (page - 1) * 100):
                raise CashCowSourceError('股票列表分页不完整或采集期间发生变化')
            for row in batch:
                code, market, name = row.get('f12'), row.get('f13'), row.get('f14')
                if (not isinstance(code, str) or not re.fullmatch(r'\d{6}', code)
                        or market not in (0, 1) or not isinstance(name, str) or code in seen):
                    raise CashCowSourceError('股票列表含重复或无效记录')
                seen.add(code)
                self.secids[code] = f'{market}.{code}'
                exchange = 'SH' if market == 1 else ('BJ' if code.startswith(('4', '8', '92')) else 'SZ')
                stocks.append(dict(code=code, name=name, exchange=exchange))
            if len(stocks) == total:
                return stocks
            page += 1
            if self.progress:
                self.progress(f'正在读取沪深京股票列表第 {page} 页')

    def _annual(self, report, year, codes):
        # Annual batches are bounded to 500 rows/page and contain delisted and
        # non-A-share issuers. Caller intersects the current quote universe.
        if codes is not None and 200 < len(codes) <= 2000:
            unique_codes = list(dict.fromkeys(codes))
            rows = []
            for offset in range(0, len(unique_codes), 200):
                self.check_cancel()
                rows.extend(self._annual(report, year, unique_codes[offset:offset + 200]))
            return rows
        filter_text = f"(REPORT_DATE='{int(year)}-12-31')"
        if report == 'RPT_F10_FINANCE_MAINFINADATA':
            filter_text += '(CURRENCY="CNY")(REPORT_TYPE="年报")'
        if codes is not None and not codes:
            return []
        if codes is not None and len(codes) <= 200:
            if any(not re.fullmatch(r'\d{6}', code) for code in codes):
                raise CashCowSourceError('证券代码格式无效')
            filter_text += '(SECURITY_CODE in (' + ','.join('\"' + code + '\"' for code in codes) + '))'
        columns = ('SECUCODE,SECURITY_CODE,REPORT_DATE,DIVIDEND_IMPLE,PARENTNETPROFIT,DIVIDEND_PAY_IMPLE'
                   if report == 'RPT_F10_DIVIDEND_HISTOGRAM' else
                   'SECUCODE,SECURITY_CODE,REPORT_DATE,KCFJCXSYJLR,PARENTNETPROFIT,NETCASH_OPERATE_PK')
        rows = self._rows(report, filter_text,
                          lambda r: r.get('SECUCODE'), columns=columns)
        if any(str(r.get('REPORT_DATE') or '')[:10] != f'{int(year)}-12-31' for r in rows):
            raise CashCowSourceError('年度报表日期缺失或与请求年份不符')
        allowed = set(codes) if codes is not None else None
        selected = [r for r in rows if str(r.get('SECUCODE', '')).endswith(('.SH', '.SZ', '.BJ'))
                    and (allowed is None or r.get('SECURITY_CODE') in allowed)]
        if len({r.get('SECURITY_CODE') for r in selected}) != len(selected):
            raise CashCowSourceError('年度报表证券代码存在歧义')
        return selected

    def dividends(self, year, codes=None):
        return {r['SECURITY_CODE']: dict(cash_dividend=number(r.get('DIVIDEND_IMPLE')),
                net_profit=number(r.get('PARENTNETPROFIT')),
                payout_ratio=number(r.get('DIVIDEND_PAY_IMPLE')))
                for r in self._annual('RPT_F10_DIVIDEND_HISTOGRAM', year, codes)}

    def financials(self, year, codes=None):
        return {r['SECURITY_CODE']: dict(adjusted_profit=number(r.get('KCFJCXSYJLR')),
                net_profit=number(r.get('PARENTNETPROFIT')),
                operating_cashflow=number(r.get('NETCASH_OPERATE_PK')))
                for r in self._annual('RPT_F10_FINANCE_MAINFINADATA', year, codes)}

    def _capital_stable(self, code, start_date):
        if code not in self.capital:
            exchange = 'SH' if code.startswith('6') else ('BJ' if code.startswith(('4', '8', '92')) else 'SZ')
            self.capital[code] = self._rows('RPT_F10_EH_EQUITY', f'(SECUCODE="{code}.{exchange}")',
                lambda r: r.get('END_DATE'), sort='END_DATE',
                columns='SECUCODE,SECURITY_CODE,END_DATE,TOTAL_SHARES,CHANGE_REASON')
        baseline = None
        for row in self.capital[code]:
            day = str(row.get('END_DATE') or '')[:10]
            shares = number(row.get('TOTAL_SHARES'))
            if not day or shares is None or shares <= 0:
                return False
            if day <= start_date:
                baseline = shares
            elif day <= self.as_of:
                if baseline is None:
                    return False
                reason = str(row.get('CHANGE_REASON') or '')
                # Verified ordinary issuance does not multiply an existing
                # holder's units. Mixed or unfamiliar reasons remain uncertain.
                unchanged_unit = reason in {
                    '增发A股上市', '增发上市', '超额配售', '回购注销',
                    '股份回购注销', '股权激励股份回购注销',
                }
                if shares != baseline and not unchanged_unit:
                    return False
                # Share-count neutral reorganizations can still change a holder's basis.
                if re.search(r'拆|合股|缩股|配股|送股|转增', str(row.get('CHANGE_REASON') or '')):
                    return False
                baseline = shares
        return baseline is not None

    def _dividend_events(self, code):
        if code not in self.events:
            self.events[code] = self._rows('RPT_F10_DIVIDEND_MAIN', f'(SECURITY_CODE="{code}")',
                lambda r: (r.get('REPORT_TIME'), r.get('NOTICE_DATE'), r.get('INFO_CODE'),
                           r.get('ASSIGN_PROGRESS'), r.get('ASSIGN_OBJECT')),
                sort='NOTICE_DATE')
        return self.events[code]

    def per_share_dividends_by_period(self, code, end_year, periods):
        # Raw events and capital history are fetched once. Share-basis validity
        # depends on each window's start, so a historical split must not poison
        # an otherwise valid shorter period.
        rows = self._dividend_events(code)
        return {period: self._annual_dividends(code, end_year - period + 1, end_year, rows)
                for period in periods}

    def per_share_dividends(self, code, start_year, end_year):
        return self._annual_dividends(code, start_year, end_year, self._dividend_events(code))

    def _annual_dividends(self, code, start_year, end_year, rows):
        annual, invalid, events = {}, set(), set()
        # Unnormalised corporate actions change the share basis across fiscal
        # and payment dates. Never compare their raw DPS with raw daily prices.
        ambiguous_action = any(corporate_action(str(r.get('IMPL_PLAN_PROFILE') or ''))
            and str(r.get('EX_DIVIDEND_DATE') or r.get('PAY_CASH_DATE') or '')[:4].isdigit()
            and start_year <= int(str(r.get('EX_DIVIDEND_DATE') or r.get('PAY_CASH_DATE'))[:4]) <= int(self.as_of[:4])
            and r.get('ASSIGN_PROGRESS') == '实施方案' for r in rows)
        for row in rows:
            fiscal = str(row.get('REPORT_TIME') or '')[:10]
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', fiscal):
                continue
            year = int(fiscal[:4])
            if not start_year <= year <= end_year or row.get('ASSIGN_PROGRESS') != '实施方案':
                continue
            paid = str(row.get('PAY_CASH_DATE') or row.get('EX_DIVIDEND_DATE') or '')[:10]
            if not paid or paid > self.as_of:
                invalid.add(year)
                continue
            text = str(row.get('IMPL_PLAN_PROFILE') or '')
            obj = row.get('ASSIGN_OBJECT')
            event = (fiscal, paid)
            # Multiple conflicting revisions/shareholder classes on one payout
            # date are not additive. Bonus/splits need share-basis adjustment.
            if event in events or obj not in ('全体股东', '全体A股股东', 'A股股东') or corporate_action(text):
                invalid.add(year)
            events.add(event)
            rate = cash_rate(text)
            if rate is None:
                invalid.add(year)
                continue
            annual[year] = annual.get(year, 0.0) + rate
        for year in invalid:
            annual[year] = None
        if ambiguous_action or not self._capital_stable(code, f'{start_year}-01-01'):
            return {year: None for year in range(start_year, end_year + 1)}
        return annual

    def prices(self, code, start_year, end_year):
        secid = self.secids.get(code, f'{1 if code.startswith("6") else 0}.{code}')
        payload = self._cached('daily', [code])
        from_cache = isinstance(payload, dict)
        if not from_cache:
            payload = self._get(PRICES, dict(secid=secid, klt=101, fqt=0,
            beg='0', end=self.as_of.replace('-', ''), lmt=10000,
            fields1='f1,f2,f3,f4,f5,f6',
            fields2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'))
        result = payload.get('data')
        if payload.get('rc') != 0 or not isinstance(result, dict) or result.get('code') != code:
            raise CashCowSourceError('股票历史价格不可用')
        bars = result.get('klines')
        if (not isinstance(bars, list) or len(bars) >= 10000
                or type(result.get('dktotal')) is not int or len(bars) != result['dktotal']):
            raise CashCowSourceError('股票历史价格格式异常或可能被截断')
        annual, previous, daily = {}, '', []
        for bar in bars:
            fields = bar.split(',') if isinstance(bar, str) else []
            if len(fields) != 11 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', fields[0]) or fields[0] <= previous:
                raise CashCowSourceError('股票历史价格存在重复或无效日期')
            day, close = fields[0], number(fields[2])
            previous = day
            year = int(day[:4])
            if day > self.as_of:
                raise CashCowSourceError('股票历史价格超出请求范围')
            daily.append((day, close))
            if start_year <= year <= end_year:
                annual.setdefault(year, []).append((day, close))
        if not from_cache:
            self._save_cache('daily', [code], payload)
        self.daily[code] = daily
        output = {}
        for year, observations in annual.items():
            # A short first/last year cannot stand in for a complete fiscal year.
            # A suspended year with no January/December observation is withheld.
            valid = (observations[0][0].startswith(f'{year}-01-')
                     and observations[-1][0].startswith(f'{year}-12-')
                     and all(v is not None and v > 0 for _, v in observations))
            output[year] = dict(year_average_close=(sum(v for _, v in observations) / len(observations)) if valid else None,
                                price_date=observations[-1][0], trading_days=len(observations))
        return output

    def current_dividend_yield(self, code):
        if code not in self.events:
            self.per_share_dividends(code, int(self.as_of[:4]) - 2, int(self.as_of[:4]))
        if code not in self.daily:
            self.prices(code, int(self.as_of[:4]) - 1, int(self.as_of[:4]))
        today = date.fromisoformat(self.as_of)
        try:
            cutoff = str(today.replace(year=today.year - 1))
        except ValueError:  # February 29 to prior February 28.
            cutoff = str(today.replace(year=today.year - 1, day=28))
        total, seen, valid = 0.0, set(), self._capital_stable(code, cutoff)
        for row in self.events[code]:
            if row.get('ASSIGN_PROGRESS') != '实施方案':
                continue
            text = str(row.get('IMPL_PLAN_PROFILE') or '').strip()
            rate = cash_rate(text)
            # Known non-cash/zero-cash plans need no cash payment date. Their
            # unit-change risk is independently checked by capital history.
            if (text in ('不分配不转增', '不分配') or rate == 0 or
                    (corporate_action(text) and '派' not in text and '现金' not in text)):
                continue
            paid = str(row.get('PAY_CASH_DATE') or row.get('EX_DIVIDEND_DATE') or '')[:10]
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', paid):
                valid = False
                continue
            if not cutoff < paid <= self.as_of:
                continue
            if (rate is None or paid in seen or row.get('ASSIGN_OBJECT') not in
                    ('全体股东', '全体A股股东', 'A股股东')):
                valid = False
            elif rate is not None:
                total += rate
            seen.add(paid)
        daily = self.daily[code]
        day, close = daily[-1] if daily else (None, None)
        # A long-suspended quote is not described as a current quote.
        fresh = day and (date.fromisoformat(self.as_of) - date.fromisoformat(day)).days <= 10
        return dict(current_yield=100 * total / close if valid and fresh and close and close > 0 else None,
                    latest_price=close, latest_price_date=day,
                    dividend_ttm_per_share=total if valid else None)
