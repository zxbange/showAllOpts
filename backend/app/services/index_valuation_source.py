"""Dated Eastmoney valuations; Tencent unadjusted index-close transport fallback.

Current constituents are never used to invent past rosters. Quote provenance is
logged separately; the existing source identifier denotes the PE/member dataset.
"""
from datetime import date, timedelta
from decimal import Decimal
import json
import logging
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import httpx

from app.core.diagnostics import diagnostic_details

LOG = logging.getLogger(__name__)

from app.services.index_valuation_rules import INDEXES, PUBLIC_SOURCE, number, summarize, valid_code
from app.services.valuation_ruler_model import ValuationError

DATA_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
PRICE_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
TENCENT_URL = 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
TYPES = dict(zip(INDEXES, ('1', '2', '7')))
CHINA = ZoneInfo('Asia/Shanghai')


def source_date(value):
    if not isinstance(value, str) or len(value) < 10:
        raise ValuationError('东方财富日期缺失。')
    return date.fromisoformat(value[:10])


class EastmoneySource:
    def __init__(self, client, monitor, check):
        self.client, self.monitor, self.check = client, monitor, check

    def request(self, url, params):
        # Never log the URL query, response body or configuration credentials.
        provider = 'tencent' if url == TENCENT_URL else 'eastmoney'
        endpoint = ('index_closes' if url in (PRICE_URL, TENCENT_URL) else
                    {'RPT_INDEX_TS_COMPONENT': 'index_members',
                     'RPT_VALUEANALYSIS_DET': 'member_valuations'}.get(params.get('reportName'), 'report'))
        host, path = urlsplit(url).hostname, urlsplit(url).path
        symbol = (params.get('param', '').split(',')[0] if url == TENCENT_URL else
                  params.get('secid', '-') if url == PRICE_URL else '-')
        page = params.get('pageNumber', '-')
        for attempt in range(3):
            self.check()
            started = time.monotonic()
            response = None
            LOG.info('Valuation source request provider=%s endpoint=%s host=%s path=%s '
                     'symbol=%s page=%s attempt=%s/3', provider, endpoint, host, path, symbol, page, attempt+1)
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                if len(response.content) > 4_000_000:
                    raise ValuationError('东方财富响应超过预期范围。')
                result = json.loads(response.content, parse_float=Decimal)
                if not isinstance(result, dict):
                    raise ValuationError('东方财富响应结构无效。')
                self.check()
                LOG.info('Valuation source complete provider=%s endpoint=%s symbol=%s page=%s '
                         'attempt=%s/3 http_status=%s received_bytes=%s elapsed_seconds=%.3f',
                         provider, endpoint, symbol, page, attempt+1, response.status_code, len(response.content),
                         time.monotonic()-started)
                return result
            except (httpx.HTTPError, ValueError) as exc:
                transient = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, httpx.HTTPStatusError) and
                    (exc.response.status_code == 429 or exc.response.status_code >= 500))
                LOG.warning('Valuation source failed provider=%s endpoint=%s host=%s path=%s '
                            'symbol=%s page=%s attempt=%s/3 http_status=%s received_bytes=%s '
                            'elapsed_seconds=%.3f retryable=%s %s',
                            provider, endpoint, host, path, symbol, page, attempt+1,
                            response.status_code if response is not None else '-',
                            len(response.content) if response is not None else 0,
                            time.monotonic()-started, transient, diagnostic_details(exc))
                if not transient or attempt == 2:
                    labels = {'index_closes': '指数日线', 'index_members': '指数成分',
                              'member_valuations': '成分股估值', 'report': '数据'}
                    raise ValuationError('{}{}读取失败，已有数据保留；请查看采集日志。'.format(
                        '腾讯' if provider == 'tencent' else '东方财富', labels[endpoint])) from exc
                self.monitor.stop_event.wait(attempt+1)
                self.check()

    def report(self, name, fields, filters, expected=None):
        rows, count, pages = [], None, None
        for page in range(1, 11):
            response = self.request(DATA_URL, dict(reportName=name, columns=fields, filter=filters,
                pageSize=500, pageNumber=page, sortColumns='SECURITY_CODE', sortTypes=1))
            result = response.get('result')
            if response.get('success') is not True or not isinstance(result, dict):
                raise ValuationError('东方财富尚未返回完整估值数据，稍后自动重试。')
            if page == 1:
                count, pages = result.get('count'), result.get('pages')
                if (type(count) is not int or not 1 <= count <= 5000 or type(pages) is not int
                        or pages != (count+499)//500 or (expected is not None and count != expected)):
                    raise ValuationError('东方财富成分或估值条数不完整。')
            batch = result.get('data')
            if (result.get('count') != count or result.get('pages') != pages or not isinstance(batch, list)
                    or len(batch) != min(500, count-len(rows))):
                raise ValuationError('东方财富分页数据不完整，稍后自动重试。')
            rows.extend(batch)
            if page == pages:
                return rows
        raise ValuationError('东方财富分页超出预期。')

    def closes(self, code, cutoff):
        try:
            response = self.request(PRICE_URL, dict(secid='1.'+code[:6], klt=101, fqt=0,
                beg=(cutoff-timedelta(days=45)).strftime('%Y%m%d'), end=cutoff.strftime('%Y%m%d'), lmt=40,
                fields1='f1,f2,f3,f4,f5,f6', fields2='f51,f52,f53,f54,f55,f56'))
        except ValuationError as exc:
            # Do not hide invalid, stale or inconsistent vendor data with a fallback.
            cause = exc.__cause__
            transient = isinstance(cause, httpx.TransportError) or (
                isinstance(cause, httpx.HTTPStatusError) and
                (cause.response.status_code == 429 or cause.response.status_code >= 500))
            if not transient:
                raise
            LOG.warning('Valuation close fallback index=%s cutoff=%s from=eastmoney to=tencent', code, cutoff)
            return self.tencent_closes(code, cutoff)
        data = response.get('data')
        if response.get('rc') != 0 or not isinstance(data, dict) or data.get('code') != code[:6] or data.get('market') != 1:
            raise ValuationError('东方财富指数收盘行情缺失。')
        rows = data.get('klines')
        if not isinstance(rows, list) or not rows:
            raise ValuationError('东方财富指数日线为空。')
        values = {}
        for row in rows:
            if not isinstance(row, str) or len(row.split(',')) < 3:
                raise ValuationError('东方财富指数日线记录不完整。')
            parts = row.split(',')
            day = source_date(parts[0])
            close = number(parts[2], 24, 8)
            if day > cutoff or day.weekday() >= 5 or close <= 0 or (values and day <= max(values)):
                raise ValuationError('东方财富指数日线日期或价格无效。')
            values[day] = close
        LOG.info('Valuation close selected provider=eastmoney index=%s start=%s end=%s rows=%s adjustment=none',
                 code, min(values), max(values), len(values))
        return values

    def tencent_closes(self, code, cutoff):
        if code not in INDEXES:
            raise ValuationError('指数不在支持范围。')
        start = cutoff-timedelta(days=45)
        symbol = 'sh'+code[:6]
        payload = self.request(TENCENT_URL, {'param': '{},day,{},{},50,'.format(symbol, start, cutoff)})
        data = payload.get('data')
        item = data.get(symbol) if isinstance(data, dict) else None
        rows = item.get('day') if isinstance(item, dict) else None
        if payload.get('code') not in (0, '0') or not isinstance(rows, list) or not rows or len(rows) > 640:
            raise ValuationError('腾讯未返回指定指数的不复权日线。')
        values, previous = {}, None
        for row in rows:
            if (not isinstance(row, list) or len(row) < 3 or
                    not isinstance(row[0], str) or len(row[0]) != 10):
                raise ValuationError('腾讯指数日线记录不完整。')
            day = source_date(row[0])
            close = number(row[2], 24, 8)
            if day > cutoff or day.weekday() >= 5 or close <= 0 or (previous and day <= previous):
                raise ValuationError('腾讯指数日线日期或价格无效。')
            previous = day
            # Tencent may include rows earlier than the requested start.
            if day >= start:
                values[day] = close
        if not values or (cutoff.weekday() < 5 and max(values) != cutoff):
            raise ValuationError('腾讯目标日收盘行情尚未发布，已有数据保留。')
        LOG.info('Valuation close selected provider=tencent index=%s start=%s end=%s rows=%s adjustment=none',
                 code, min(values), max(values), len(values))
        return values

    def snapshot(self, code, day):
        rows = self.report('RPT_INDEX_TS_COMPONENT', 'SECUCODE,TYPE,SECURITY_CODE,MAXTRADEDATE',
                           '(TYPE="'+TYPES[code]+'")', INDEXES[code][1])
        codes = []
        for row in rows:
            security = row['SECUCODE'].replace('.SH', '.XSHG').replace('.SZ', '.XSHE')
            if (not valid_code(security) or row['SECURITY_CODE'] != security[:6]
                    or str(row['TYPE']) != TYPES[code] or source_date(row['MAXTRADEDATE']) != day):
                raise ValuationError('东方财富最新成分日期与目标交易日不一致；不使用当前名单回填历史。')
            codes.append(security)
        if len(set(codes)) != len(codes):
            raise ValuationError('东方财富成分重复。')
        return sorted(codes)

    def valuations(self, codes, day):
        values = {}
        ordered = sorted(codes)
        for offset in range(0, len(ordered), 200):
            batch = ordered[offset:offset+200]
            filters = "(TRADE_DATE='{}')(SECURITY_CODE in ({}))".format(day,
                ','.join('"'+code[:6]+'"' for code in batch))
            rows = self.report('RPT_VALUEANALYSIS_DET',
                'SECURITY_CODE,SECUCODE,TRADE_DATE,PE_TTM,PE_LAR,TOTAL_MARKET_CAP', filters, len(batch))
            for row in rows:
                code = row['SECUCODE'].replace('.SH', '.XSHG').replace('.SZ', '.XSHE')
                if code not in batch or code in values or row['SECURITY_CODE'] != code[:6] or source_date(row['TRADE_DATE']) != day:
                    raise ValuationError('东方财富个股估值日期、代码或重复记录异常。')
                def pe(field):
                    value = row[field]
                    # A disclosed null/zero PE is unavailable, never a profitable observation.
                    if value is None or value == 0:
                        return None
                    return number(value, 30, 12, round_source=True)
                cap = number(row['TOTAL_MARKET_CAP'], 30, 12)
                if cap <= 0:
                    raise ValuationError('东方财富个股总市值缺失或无效。')
                values[code] = dict(code=code, pe_ttm=pe('PE_TTM'), pe_lyr=pe('PE_LAR'),
                    market_cap=number(cap/Decimal(100000000), 30, 12, round_source=True), suspended_listing=None)
        return values

    def collect_latest(self, cutoff, moment):
        """Archive the same-day roster before value requests, so delayed values can be retried."""
        prices = {code: self.closes(code, cutoff) for code in INDEXES}
        targets = {max(values) for values in prices.values()}
        if len(targets) != 1:
            raise ValuationError('三个指数的最新交易日尚未对齐。')
        day = targets.pop()
        # A stale quote on a weekday must not complete today's scheduled slot.
        # Holidays may retry harmlessly; no weekday is invented as a trading day.
        if cutoff.weekday() < 5 and day != cutoff:
            raise ValuationError('目标日收盘行情尚未发布，稍后自动重试。')
        # The roster endpoint returns only the latest membership. For an
        # earlier date require its archived roster instead of querying current
        # constituents and risking a mislabeled historical valuation.
        historical = day < moment.astimezone(CHINA).date()
        rosters = None if historical else {code: self.snapshot(code, day) for code in INDEXES}
        with self.monitor.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS index_rosters_v2 '
                       '(trade_date TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            old = db.execute('SELECT payload FROM index_rosters_v2 WHERE trade_date=?', (str(day),)).fetchone()
            if historical:
                if not old:
                    raise ValuationError('目标交易日没有已核验的成分存档；不使用当前名单回填。')
            else:
                payload = json.dumps(rosters, sort_keys=True)
                if old and old[0] != payload:
                    raise ValuationError('同日成分名单变化，已保留首次快照；请核实后补采。')
                db.execute('INSERT OR IGNORE INTO index_rosters_v2 VALUES (?,?)', (str(day), payload))
            pending = [(date.fromisoformat(r[0]), json.loads(r[1])) for r in db.execute(
                'SELECT trade_date,payload FROM index_rosters_v2 WHERE trade_date<=? ORDER BY trade_date', (str(day),))]
        result = []
        for target, roster in pending:
            if any(target not in values for values in prices.values()):
                raise ValuationError('待补日期超过自动行情窗口，请使用聚宽历史补采。')
            values = self.valuations({c for members in roster.values() for c in members}, target)
            result.extend(summarize(target, prices[code][target], [values[c] for c in roster[code]],
                                    code, source=PUBLIC_SOURCE) for code in INDEXES)
        return result
