#!/usr/bin/env python3
"""Exchange-published ETF shares and daily closes. Dry-run unless --write.

Shares: SSE ETF scale disclosure / SZSE fund scale report (10,000 shares).
Prices: exchange daily K-lines; missing history is reported, never synthesized.
"""
from __future__ import annotations

import argparse
import calendar as month_calendar
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import sys
import time
import logging
from zoneinfo import ZoneInfo

import httpx

from app.core.etf_catalog import ETF_GROUPS
from app.core.diagnostics import diagnostic_details

LOG = logging.getLogger(__name__)

SSE_SHARES = 'https://query.sse.com.cn/commonQuery.do'
SZSE_SHARES = 'https://www.szse.cn/api/report/ShowReport/data'
SSE_PRICE = 'https://yunhq.sse.com.cn:32042/v1/sh1/dayk/'
SZSE_QUOTE = 'https://www.szse.cn/api/market/ssjjhq/getTimeData'
SZSE_PRICE = 'https://www.szse.cn/api/market/ssjjhq/getHistoryData'
SZSE_CALENDAR = 'https://www.szse.cn/api/report/exchange/onepersistenthour/monthList'


class SourceError(ValueError):
    """Invalid or incomplete exchange response; no partial transaction is written."""


def parse_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        raw = str(value).strip()
        if len(raw) == 8 and raw.isdigit():
            return datetime.strptime(raw, '%Y%m%d').date()
        if len(raw) != 10:
            raise ValueError
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        raise SourceError('Invalid exchange date') from None


class ExchangeCalendar:
    """Complete observed SSE index sessions, never inferred weekdays."""
    source = 'SSE.INDEX_TRADING_DAYS'

    def __init__(self, days, snapshot_day, snapshot_time, now=None, official_days=None):
        self.now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
        if not days or days != sorted(set(days)):
            raise SourceError('Trading calendar dates must be unique and strictly increasing')
        if days[-1] > self.now.date() or snapshot_day > self.now.date():
            raise SourceError('Trading calendar contains future dates')
        self.official_days = official_days or {}
        if days[-1] != snapshot_day:
            # During trading, the snapshot can already be today's while dayk
            # still contains only completed sessions. Admit precisely the prior
            # official session, never an arbitrary older or truncated tail.
            intraday_completed_only = (
                self.now.hour < 15 and snapshot_day == self.now.date()
                and self.official_days.get(snapshot_day) is True
                and days[-1] == self.latest_completed_day()
            )
            if not intraday_completed_only:
                raise SourceError('Trading calendar tail disagrees with official index snapshot')
        self.snapshot_day = snapshot_day
        self.snapshot_time = snapshot_time
        self.days = tuple(day for day in days if day < self.now.date() or
                          (self.now.hour >= 15 and snapshot_time >= 150000))
        if not self.days:
            raise SourceError('Trading calendar has no confirmed completed sessions')
        self.before = dict(zip(self.days[1:], self.days[:-1]))
        observed = set(days)
        for day, is_open in self.official_days.items():
            if days[0] <= day <= days[-1] and is_open != (day in observed):
                raise SourceError('Dynamic trading calendar disagrees with official monthly calendar')

    def latest_completed_day(self):
        """Resolve the expected close solely from published open/closed markers."""
        target = self.now.date()
        if self.now.hour < 15:
            target -= timedelta(days=1)
        while target in self.official_days and not self.official_days[target]:
            target -= timedelta(days=1)
        if target not in self.official_days:
            raise SourceError('Official current-month trading calendar is unavailable; freshness cannot be verified')
        return target

    def require_fresh(self):
        """Monthly official open/closed markers distinguish holidays from stale bars."""
        target = self.latest_completed_day()
        if self.days[-1] != target:
            raise SourceError('Official trading calendar is stale for the latest completed session')
        if target == self.now.date() and self.snapshot_time < 150000:
            raise SourceError('Official index snapshot has not reached today close')

    def range(self, start, end):
        start, end = parse_date(start), parse_date(end)
        if start > end:
            raise SourceError('Start date must not exceed end date')
        if end > self.now.date():
            raise SourceError('End date must not be in the future')
        if start < self.days[0] or end > self.days[-1]:
            raise SourceError(f'Requested dates exceed confirmed trading calendar coverage {self.days[0]}..{self.days[-1]}')
        return [day for day in self.days if start <= day <= end]

    def previous(self, day):
        if day not in self.before:
            raise SourceError('Previous trading session is outside verified calendar coverage')
        return self.before[day]

    def default_range(self, lookback=5):
        if lookback < 1 or lookback > len(self.days):
            raise SourceError('Lookback exceeds confirmed calendar coverage')
        self.require_fresh()
        return self.days[-lookback], self.days[-1]

    def records(self, *, target=None):
        # Preserve the predecessor mapping from the full verified calendar,
        # even for the previous row when its own prior date is not written.
        days = self.days if target is None else (self.previous(target), target)
        stamp = self.now.replace(tzinfo=None)
        return [dict(trade_date=day, previous_trade_date=self.before.get(day),
                     source=self.source, updated_at=stamp) for day in days]


def decimal_value(value):
    try:
        number = Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        raise SourceError('Invalid exchange numeric value') from None
    if not number.is_finite():
        raise SourceError('Non-finite exchange numeric value')
    return number


def normalize_shares(value):
    number = decimal_value(value) * 10000
    if number < 0 or number != number.to_integral_value() or number > 9223372036854775807:
        raise SourceError('Shares must be nonnegative integral units within BIGINT')
    return int(number)


def normalize_close(value):
    number = decimal_value(value)
    if number <= 0 or number >= Decimal('1000000000000') or number != number.quantize(Decimal('.00000001')):
        raise SourceError('Close must be positive and fit NUMERIC(20,8)')
    return number


def catalog(groups=None):
    result = {}
    for members in (ETF_GROUPS if groups is None else groups).values():
        for code, name in members:
            if code in result and result[code] != name:
                raise SourceError('Conflicting ETF catalog names')
            if len(code) != 11 or not code[:6].isdigit() or code[6:] not in ('.XSHG', '.XSHE'):
                raise SourceError('Invalid ETF catalog code')
            result[code] = name
    return result


class ExchangeClient:
    def __init__(self, client=None, attempts=3, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
        self.attempts = attempts
        self.sleep = sleep

    def get(self, url, params, referer):
        for attempt in range(self.attempts):
            try:
                response = self.client.get(url, params=params, headers={'Referer': referer})
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as error:
                source = {SSE_SHARES: 'sse_shares', SZSE_SHARES: 'szse_shares',
                          SSE_PRICE: 'sse_price', SZSE_QUOTE: 'szse_quote',
                          SZSE_PRICE: 'szse_price', SZSE_CALENDAR: 'szse_calendar'}.get(url, 'exchange')
                LOG.warning('ETF source request failed source=%s attempt=%s/%s %s',
                            source, attempt + 1, self.attempts, diagnostic_details(error))
                if attempt + 1 == self.attempts:
                    raise SourceError('Exchange request failed after bounded retries') from None
                self.sleep(2 ** attempt)

    def month_calendar(self, month, today=None):
        today = today or datetime.now(ZoneInfo('Asia/Shanghai')).date()
        month = month.replace(day=1)
        payload = self.get(SZSE_CALENDAR, {'month': month.strftime('%Y-%m')}, 'https://www.szse.cn/')
        if not isinstance(payload, dict) or parse_date(payload.get('nowdate')) != today:
            raise SourceError('Official monthly calendar response is stale')
        rows = payload.get('data')
        expected = {month + timedelta(days=offset) for offset in range(month_calendar.monthrange(month.year, month.month)[1])}
        result = {}
        if not isinstance(rows, list) or not rows:
            raise SourceError('Official monthly calendar is unpublished or unavailable')
        for row in rows:
            if not isinstance(row, dict) or str(row.get('jybz')) not in ('0', '1'):
                raise SourceError('Invalid official monthly calendar open/closed marker')
            day = parse_date(row.get('jyrq'))
            if day in result or day not in expected:
                raise SourceError('Duplicate or out-of-month official calendar date')
            result[day] = str(row['jybz']) == '1'
        if set(result) != expected:
            raise SourceError('Official monthly calendar is incomplete')
        return result

    def calendar(self):
        payload = self.get(SSE_PRICE + '000001', {'select': 'date', 'begin': 0, 'end': -1}, 'https://www.sse.com.cn/')
        if not isinstance(payload, dict) or str(payload.get('code')) != '000001':
            raise SourceError('Official calendar index code mismatch')
        rows = payload.get('kline')
        if not isinstance(rows, list) or not rows or any(not isinstance(row, list) or len(row) != 1 for row in rows):
            raise SourceError('Official calendar response schema changed')
        if count_value(payload.get('begin')) != 0 or count_value(payload.get('total')) != len(rows) or count_value(payload.get('end')) != len(rows):
            raise SourceError('Official trading calendar is truncated')
        days = [parse_date(row[0]) for row in rows]
        # The index's first published daily record is independently known. A rolling
        # response must not silently become a full-history calendar.
        if days[0] != date(1990, 12, 19):
            raise SourceError('Official trading calendar initial-history boundary changed')
        snapshot = self.get(SSE_PRICE.replace('/dayk/', '/snap/') + '000001',
                            {'select': 'date,time'}, 'https://www.sse.com.cn/')
        if not isinstance(snapshot, dict) or str(snapshot.get('code')) != '000001':
            raise SourceError('Official calendar snapshot code mismatch')
        values = snapshot.get('snap')
        if not isinstance(values, list) or len(values) != 2 or parse_date(values[0]) != parse_date(snapshot.get('date')):
            raise SourceError('Official calendar snapshot date mismatch')
        snapshot_time = count_value(values[1])
        if snapshot_time > 235959 or snapshot_time % 100 > 59 or snapshot_time // 100 % 100 > 59:
            raise SourceError('Official calendar snapshot time invalid')
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        official_days = self.month_calendar(now.date().replace(day=1), now.date())
        previous_month = (now.date().replace(day=1) - timedelta(days=1)).replace(day=1)
        official_days.update(self.month_calendar(previous_month, now.date()))
        result = ExchangeCalendar(days, parse_date(values[0]), snapshot_time, now=now, official_days=official_days)
        result.require_fresh()
        return result

    def sse_shares(self, day):
        records, expected = [], None
        for page in range(1, 1001):
            payload = self.get(SSE_SHARES, {
                'sqlId': 'COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L', 'STAT_DATE': day.isoformat(),
                'isPagination': 'true', 'pageHelp.pageSize': 1000, 'pageHelp.cacheSize': 1,
                'pageHelp.pageNo': page,
            }, 'https://www.sse.com.cn/')
            if not isinstance(payload, dict) or payload.get('actionErrors') or payload.get('fieldErrors'):
                raise SourceError('SSE share response error')
            info = payload.get('pageHelp') or {}
            rows = payload.get('result')
            if rows is None:
                rows = info.get('data')
            total = count_value(info.get('total'))
            pages = count_value(info.get('pageCount'))
            if not isinstance(rows, list) or count_value(info.get('pageNo')) != page:
                raise SourceError('SSE pagination schema changed')
            if expected is None:
                expected = (total, pages)
            if expected != (total, pages):
                raise SourceError('SSE report changed during pagination; retry whole run')
            records.extend(rows)
            if page >= pages:
                if len(records) != total:
                    raise SourceError('SSE report truncated')
                return records
            if not rows:
                raise SourceError('SSE empty intermediate page')
        raise SourceError('SSE pagination limit exceeded')

    def szse_shares(self, code, start, end):
        records, expected = [], None
        for page in range(1, 1001):
            payload = self.get(SZSE_SHARES, {'SHOWTYPE': 'JSON', 'CATALOGID': 'scsj_fund_jjgm',
                'TABKEY': 'tab1', 'txtStart': start.isoformat(), 'txtEnd': end.isoformat(),
                'jjlb': 'ETF', 'txtDm': code[:6], 'PAGENO': page}, 'https://www.szse.cn/')
            if not isinstance(payload, list):
                raise SourceError('SZSE report schema changed')
            reports = [p for p in payload if isinstance(p, dict) and p.get('metadata', {}).get('tabkey') == 'tab1']
            if len(reports) != 1 or reports[0].get('error'):
                raise SourceError('SZSE report error')
            report = reports[0]
            info, rows = report['metadata'], report.get('data')
            total, pages = count_value(info.get('recordcount')), count_value(info.get('pagecount'))
            if not isinstance(rows, list) or count_value(info.get('pageno')) != page:
                raise SourceError('SZSE pagination schema changed')
            if expected is None:
                expected = (total, pages)
            if expected != (total, pages):
                raise SourceError('SZSE report changed during pagination; retry whole run')
            records.extend(rows)
            if page >= pages:
                if len(records) != total:
                    raise SourceError('SZSE report truncated')
                return records
            if not rows:
                raise SourceError('SZSE empty intermediate page')
        raise SourceError('SZSE pagination limit exceeded')

    def prices(self, code):
        if code.endswith('.XSHG'):
            payload = self.get(SSE_PRICE + code[:6], {'select': 'date,close',
                'begin': 0, 'end': -1}, 'https://www.sse.com.cn/')
            if not isinstance(payload, dict) or str(payload.get('code')).strip() != code[:6]:
                raise SourceError('SSE daily price code mismatch')
            rows = payload.get('kline')
            close_index, source = 1, 'SSE.DAY_KLINE'
        else:
            payload = self.get(SZSE_PRICE, {'marketId': 1, 'code': code[:6], 'cycleType': 32},
                'https://www.szse.cn/')
            data = payload.get('data', {}) if isinstance(payload, dict) else {}
            if not isinstance(payload, dict) or str(payload.get('code')) != '0' or str(data.get('code')).strip() != code[:6]:
                raise SourceError('SZSE daily price code mismatch')
            rows = data.get('picupdata')
            close_index, source = 2, 'SZSE.DAY_KLINE'
        if not isinstance(rows, list):
            raise SourceError('Invalid daily price rows')
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, list) or len(row) <= close_index:
                raise SourceError('Invalid daily price row')
            day = parse_date(row[0])
            if day in seen:
                raise SourceError('Duplicate daily price date')
            seen.add(day)
            result.append(dict(fund_code=code, trade_date=day, close_price=normalize_close(row[close_index]), source=source))
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        # A live day bar is not a confirmed close, even when the API names it close.
        result = [r for r in result if r['trade_date'] < now.date() or
                  (r['trade_date'] == now.date() and now.hour >= 15)]
        if code.endswith('.XSHE'):
            # Adjustment semantics of historical SZSE K-lines are not verified.
            # Admit only a close cross-checked against the same day's post-close quote.
            quote = self.get(SZSE_QUOTE, {'marketId': 1, 'code': code[:6]}, 'https://www.szse.cn/')
            data = quote.get('data', {}) if isinstance(quote, dict) else {}
            if not isinstance(quote, dict) or str(quote.get('code')) != '0' or str(data.get('code')).strip() != code[:6]:
                raise SourceError('SZSE quote code mismatch')
            try:
                timestamp = datetime.fromisoformat(quote['datetime'])
            except (KeyError, ValueError, TypeError):
                raise SourceError('SZSE quote timestamp invalid') from None
            if timestamp.hour < 15:
                return []
            close = normalize_close(data.get('now'))  # data.close is yesterday's close.
            result = [r for r in result if r['trade_date'] == timestamp.date()]
            if any(r['close_price'] != close for r in result):
                raise SourceError('SZSE daily close disagrees with same-day post-close quote')
            for record in result:
                record['source'] = 'SZSE.CLOSE_VERIFIED'
        return result


def count_value(value):
    if isinstance(value, bool):
        raise SourceError('Invalid pagination count')
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise SourceError('Missing pagination count') from None
    if str(number) != str(value) or number < 0:
        raise SourceError('Invalid pagination count')
    return number


def collect(client, start, end, funds=None, include_prices=True, include_shares=True, calendar=None):
    funds = catalog() if funds is None else funds
    if parse_date(end) > datetime.now(ZoneInfo('Asia/Shanghai')).date():
        raise SourceError('End date must not be in the future')
    calendar = calendar or client.calendar()
    days = calendar.range(start, end)
    allowed = set(days)
    before = {day: calendar.previous(day) for day in days}
    shares, prices, seen = [], [], set()
    now = datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)

    def add_share(raw, suffix, requested=None):
        fields = ('SEC_CODE', 'STAT_DATE', 'TOT_VOL') if suffix == '.XSHG' else ('fund_code', 'size_date', 'current_size')
        if not isinstance(raw, dict) or any(field not in raw for field in fields):
            raise SourceError('Share row schema changed')
        code = str(raw[fields[0]]).strip() + suffix
        day = parse_date(raw[fields[1]])
        if day not in allowed or (requested is not None and day != requested):
            raise SourceError('Exchange returned a share date outside requested trading days')
        key = code, day
        if key in seen:
            raise SourceError('Duplicate share code/date in exchange response')
        seen.add(key)
        if code in funds:
            shares.append(dict(fund_code=code, fund_name=funds[code], trade_date=day,
                shares=normalize_shares(raw[fields[2]]), previous_trade_date=before[day],
                source='SSE.ETF_SCALE' if suffix == '.XSHG' else 'SZSE.FUND_SCALE', updated_at=now))

    if include_shares:
        if any(c.endswith('.XSHG') for c in funds):
            for day in days:
                for raw in client.sse_shares(day):
                    add_share(raw, '.XSHG', day)
        # Bound windows below the SZSE report's six-month UI range.
        for code in funds:
            if not code.endswith('.XSHE') or not days:
                continue
            window = days[0]
            while window <= days[-1]:
                last = min(window + timedelta(days=89), days[-1])
                for raw in client.szse_shares(code, window, last):
                    if str(raw.get('fund_code', '')).strip() != code[:6]:
                        raise SourceError('SZSE share code mismatch')
                    row_day = parse_date(raw.get('size_date'))
                    if not window <= row_day <= last:
                        raise SourceError('SZSE share date outside request window')
                    add_share(raw, '.XSHE')
                window = last + timedelta(days=1)
    if include_prices:
        for code in funds:
            for row in client.prices(code):
                if row['trade_date'] in allowed:
                    prices.append(dict(row, updated_at=now))
    share_keys = {(r['fund_code'], r['trade_date']) for r in shares}
    price_keys = {(r['fund_code'], r['trade_date']) for r in prices}
    expected = {(code, day) for code in funds for day in days}
    missing = {'shares': sorted(expected - share_keys) if include_shares else [],
               'prices': sorted(expected - price_keys) if include_prices else []}
    return shares, prices, missing


# Bound key parameters and each executemany batch for both MySQL and the
# offline SQLite path. A normal scheduled run needs one request per data table.
_WRITE_BATCH_SIZE = 250


def _write_batches(records):
    for start in range(0, len(records), _WRITE_BATCH_SIZE):
        yield records[start:start + _WRITE_BATCH_SIZE]


def _read_existing(connection, table, records, *, historical_calendar=False):
    from sqlalchemy import select, tuple_

    keys = tuple(table.primary_key.columns)
    columns = tuple(column for column in table.columns if column.name != 'updated_at')
    query = select(*columns)
    if not records:
        return {}
    if historical_calendar:
        # Compare the complete *verified* coverage, not just MAX(date). A
        # sparse legacy baseline or a bad historical predecessor must still
        # be healed. Do not read or delete dates outside this source's range.
        day = table.c.trade_date
        days = [record['trade_date'] for record in records]
        statements = [query.where(day.between(min(days), max(days)))]
    elif len(keys) == 1:
        key = keys[0]
        statements = (query.where(key.in_([record[key.name] for record in batch]))
                      for batch in _write_batches(records))
    else:
        statements = (query.where(tuple_(*keys).in_([
            tuple(record[key.name] for key in keys) for record in batch
        ])) for batch in _write_batches(records))
    return {tuple(row[key.name] for key in keys): row
            for statement in statements for row in connection.execute(statement).mappings()}


def _record_changes(table, records, existing):
    keys = tuple(table.primary_key.columns)
    inserts, updates = [], []
    for record in records:
        identity = tuple(record[key.name] for key in keys)
        old = existing.get(identity)
        if old is None:
            inserts.append(record)
        elif any(old[field] != value for field, value in record.items() if field != 'updated_at'):
            updates.append(record)
    return inserts, updates


def _flush_records(connection, table, inserts, updates):
    from sqlalchemy import and_, bindparam

    for batch in _write_batches(inserts):
        connection.execute(table.insert(), batch)
    if updates:
        keys = tuple(table.primary_key.columns)
        values = {column.name: bindparam(column.name)
                  for column in table.columns if not column.primary_key}
        statement = table.update().where(and_(*(
            key == bindparam('_etf_key_' + key.name) for key in keys
        ))).values(values)
        for batch in _write_batches(updates):
            parameters = [({name: record[name] for name in values} | {
                '_etf_key_' + key.name: record[key.name] for key in keys
            }) for record in batch]
            connection.execute(statement, parameters)


@contextmanager
def _write_transaction(connection):
    transaction = connection.begin()
    try:
        yield transaction
    except BaseException:
        if transaction.is_active:
            try:
                transaction.rollback()
            except Exception as cleanup_error:
                LOG.warning('ETF database rollback failed type=%s', type(cleanup_error).__name__)
        raise


def write_records(engine, shares, prices, calendar=None, *, calendar_target=None):
    from app.models.etf import ETFPrice, ETFShare, ETFTradingDay
    from app.services.etf_diagnostics import database_phase

    batches = [(ETFShare.__table__, list(shares)), (ETFPrice.__table__, list(prices))]
    if calendar is not None:
        days = calendar.records() if calendar_target is None else calendar.records(target=calendar_target)
        batches.append((ETFTradingDay.__table__, days))
    elif calendar_target is not None:
        raise ValueError('ETF daily writes require a verified calendar')
    changed_count = 0
    with database_phase('connect'):
        connection = engine.connect()
    try:
        # MySQL CREATE auto-commits, so run checkfirst and any initial DDL in
        # its own transaction before the atomic data write. One checkout for
        # all existing tables and both phases.
        with database_phase('schema_check'):
            with _write_transaction(connection) as transaction:
                ETFShare.metadata.create_all(connection, tables=[table for table, _ in batches], checkfirst=True)
                transaction.commit()
        with _write_transaction(connection) as transaction:
            plan = []
            for table, records in batches:
                is_calendar = table is ETFTradingDay.__table__
                phase = ('load_calendar' if is_calendar else
                         'load_share_keys' if table is ETFShare.__table__ else 'load_price_keys')
                with database_phase(phase, table.name, rows=len(records)):
                    existing = _read_existing(connection, table, records,
                                              historical_calendar=is_calendar and calendar_target is None)
                    inserts, updates = _record_changes(table, records, existing)
                plan.append((table, inserts, updates))
                if not is_calendar:
                    changed_count += len(inserts) + len(updates)
            for table, inserts, updates in plan:
                if inserts or updates:
                    with database_phase('flush', table.name, rows=len(inserts) + len(updates)):
                        _flush_records(connection, table, inserts, updates)
            with database_phase('commit'):
                transaction.commit()
    finally:
        # close() may try to rollback a broken socket. Preserve an original
        # failure or a known successful business commit, rather than treating
        # a pool cleanup exception as a new write failure.
        try:
            connection.close()
        except Exception as cleanup_error:
            LOG.warning('ETF database close failed type=%s', type(cleanup_error).__name__)

    counts = {table.name: len(inserts) + len(updates) for table, inserts, updates in plan}
    LOG.info('ETF database write summary shares_changed=%s prices_changed=%s calendar_changed=%s',
             counts[ETFShare.__tablename__], counts[ETFPrice.__tablename__],
             counts.get(ETFTradingDay.__tablename__, 0))
    return changed_count


def main(argv=None, engine_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=parse_date)
    parser.add_argument('--end', type=parse_date)
    parser.add_argument('--lookback', type=int, default=5)
    parser.add_argument('--write', action='store_true', help='Explicitly enable database writes')
    parser.add_argument('--dry-run', '--probe', action='store_true', help='Fetch and validate only (default)')
    parser.add_argument('--shares-only', action='store_true')
    parser.add_argument('--prices-only', action='store_true')
    args = parser.parse_args(argv)
    if args.write and args.dry_run or args.shares_only and args.prices_only:
        parser.error('Conflicting mode flags')
    if (args.start is None) != (args.end is None):
        parser.error('--start and --end must be supplied together')
    try:
        with httpx.Client(timeout=30, headers={'User-Agent': 'Mozilla/5.0'}) as http:
            client = ExchangeClient(http)
            calendar = client.calendar()
            start, end = (args.start, args.end) if args.start else calendar.default_range(args.lookback)
            shares, prices, missing = collect(client, start, end,
                include_prices=not args.shares_only, include_shares=not args.prices_only, calendar=calendar)
            print(f'Confirmed calendar: {calendar.days[0]}..{calendar.days[-1]}, sessions={len(calendar.days)}')
        for kind, gaps in missing.items():
            print(f'{kind}: missing {len(gaps)} requested code/date observations; not fabricated')
            for code, day in gaps[:20]:
                print(f'  {code} {day.isoformat()}')
        if args.write:
            if engine_factory is None:
                from sqlalchemy import create_engine
                from app.core.config import Settings
                engine = create_engine(Settings().database_url, echo=False, pool_pre_ping=True,
                                       connect_args={'connect_timeout': 60})
            else:
                engine = engine_factory()
            try:
                write_records(engine, shares, prices, calendar=calendar)
            finally:
                engine.dispose()
        print(f'{"Written" if args.write else "Dry-run"}: shares={len(shares)}, prices={len(prices)}, range={start}..{end}')
        # A partial run must be distinguishable in schedulers; valid observations remain usable.
        return 2 if any(missing.values()) else 0
    except Exception as exc:
        # Never emit an exception message/traceback containing a DB connection URL or secret.
        print(f'Collection failed ({type(exc).__name__}); no successful completion reported', file=sys.stderr)
        if isinstance(exc, SourceError):
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
