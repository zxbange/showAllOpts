"""Current-day public dividend screen; unknown eligibility never becomes exclusion.

Historical recovery remains in JoinQuant. Public snapshots require same-day close,
verified source coverage and latest public fiscal DPS, never DPS*current shares.
"""
from datetime import date, datetime, time as daytime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
import re
import time

from app.services.cash_cow_source import EastmoneyCashCowSource, CashCowSourceError, DATACENTER
from app.services.system_integrations import integrations_config

LOG = logging.getLogger(__name__)
RULE_VERSION = 'dividend_dps_proposal_age3_annual_history_v10'
CHINA = timezone(timedelta(hours=8))


class DividendSourceError(ValueError):
    pass


def decimal(value, *, positive=False):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or (positive and result <= 0):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise DividendSourceError('数值缺失或无效，保留原结果。') from None


def mature(listed, day):
    try:
        listed = date.fromisoformat(listed)
        if listed > day:
            raise ValueError()
        try:
            anniversary = listed.replace(year=listed.year + 3)
        except ValueError:
            anniversary = listed.replace(year=listed.year + 3, day=28)
        return day >= anniversary
    except (ValueError, TypeError):
        raise DividendSourceError('上市日期无法核验，保留原结果。') from None


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=CHINA) if result.tzinfo is None else result.astimezone(CHINA)
    except (ValueError, TypeError):
        raise DividendSourceError('行情或停牌时间无法核验。') from None


def suspended(symbol, quote, events, day):
    """Only a full-session interval can corroborate a zero-volume suspension.

    Positive same-day trading is not a full-day suspension. Temporary halts do
    not exclude a stock which resumed trading, matching daily paused semantics.
    """
    opening = datetime.combine(day, daytime(9, 30), CHINA)
    closing = datetime.combine(day, daytime(15), CHINA)
    full_day = False
    for event in events:
        if event.get('SECUCODE') != symbol:
            continue
        start = timestamp(event.get('SUSPEND_START_TIME'))
        end = timestamp(event['SUSPEND_END_TIME']) if event.get('SUSPEND_END_TIME') else None
        if end and end < start:
            raise DividendSourceError('停牌区间冲突。')
        if start <= opening and (end is None or end >= closing):
            full_day = True
    if full_day:
        # An independently confirmed full-day halt needs no synthetic current
        # price. A stale last-trade quote is expected, not a missing valuation.
        if quote and quote.get('trade_time'):
            observed = timestamp(quote['trade_time'])
            if observed.date() > day:
                raise DividendSourceError('行情日期晚于选股日。')
            if observed.date() == day and quote.get('volume') is not None:
                volume = decimal(quote['volume'])
                if volume < 0:
                    raise DividendSourceError('成交量无效。')
                if volume > 0:
                    raise DividendSourceError('停牌清单与当日成交冲突，保留原结果。')
        return True
    if not quote:
        raise DividendSourceError('非已确认停牌股票行情缺失。')
    observed = timestamp(quote.get('trade_time'))
    if observed.date() != day or observed.time() < daytime(15):
        raise DividendSourceError('行情尚未更新到选股日收盘，保留原结果。')
    volume = decimal(quote.get('volume'))
    if volume < 0:
        raise DividendSourceError('成交量无效。')
    if volume > 0:
        return False
    raise DividendSourceError('零成交但停牌状态未核验，保留原结果。')


def rank_records(stocks, valuations, proposals, industries, day, universe_count):
    """DPS / same-day raw A-share close; AS market cap remains display-only."""
    candidates = []
    for stock in stocks:
        symbol = stock['symbol']
        valuation = valuations[symbol]
        if 'pe_ttm' not in valuation:
            raise DividendSourceError('PE 字段缺失。')
        if valuation['pe_ttm'] is None:
            continue
        pe = decimal(valuation['pe_ttm'])
        cap = decimal(valuation['total_mv'], positive=True) * 10000
        if not 0 < pe < 12:
            continue
        proposal = proposals.get(symbol)
        if proposal is None or type(proposal.get('fiscal_year')) is not int:
            raise DividendSourceError('年报分红预案覆盖未核验，保留原结果。')
        fiscal = proposal['fiscal_year']
        years = list(range(fiscal, fiscal - 3, -1))
        cash = proposal.get('annual_dps')
        if not isinstance(cash, dict) or fiscal not in cash:
            raise DividendSourceError('当选财年派息覆盖未核验，保留原结果。')
        current_dps = decimal(cash[fiscal])
        if current_dps < 0:
            raise DividendSourceError('每股派息无效。')
        close = decimal(valuation['close'], positive=True)
        rate = current_dps / close * 100
        if rate <= 3:
            continue
        industry = industries.get(symbol)
        if not industry or not re.fullmatch(r'801[0-9]{3}(?:\.SI)?', str(industry.get('l1_code', ''))):
            raise DividendSourceError('申万行业缺失，保留原结果。')
        method = 'bank' if str(industry['l1_code']).split('.')[0] == '801780' else 'value'
        code, exchange = symbol.split('.')
        jq_code = code + {'SH': '.XSHG', 'SZ': '.XSHE', 'BJ': '.XBSE'}[exchange]
        level = next((n for n in (3, 2, 1) if industry.get(f'l{n}_code')), 1)
        candidates.append(dict(method=method, code=jq_code, trade_date=day, name=stock['name'],
            industry_code=industry[f'l{level}_code'], industry_name=industry.get(f'l{level}_name'),
            industry_level=f'sw_l{level}', dividend_yield_1y=rate,
            dividend_cash_1y=None, dividend_per_share=cash[years[0]], dividend_fiscal_year=fiscal,
            dividend_yield_history=[dict(fiscal_year=y, yield_pct=float(rate) if y == fiscal else None) for y in years],
            pe_ttm=pe, pb=None if valuation.get('pb') is None else decimal(valuation['pb']),
            market_cap=cap / 100000000, close=decimal(valuation['close'], positive=True)))
    candidates.sort(key=lambda row: (-row['dividend_yield_1y'], row['code']))
    records = []
    for method, count in [('value', 25), ('bank', 10)]:
        selected = [row for row in candidates if row['method'] == method][:count]
        records.extend(dict(row, rank=i + 1) for i, row in enumerate(selected))
    return dict(trade_date=day, rule_version=RULE_VERSION, universe_count=universe_count,
                eligible_count=len(stocks), value_candidates=len(candidates),
                value_count=len(records)), records


class DividendPublicSource(EastmoneyCashCowSource):
    def __init__(self, client, *, settings, as_of, cache_dir=None, check_cancel=lambda: None):
        super().__init__(client, as_of=as_of, cache_dir=cache_dir, check_cancel=check_cancel)
        self.day = date.fromisoformat(str(as_of)[:10])
        self.settings = settings
        self.token = integrations_config(settings)['asharehub']['token']
        if not self.token:
            raise DividendSourceError('请在系统配置中填写 AShareHub 凭据。')

    def ash(self, endpoint, params):
        self.check_cancel()
        time.sleep(max(0, 2 - (time.monotonic() - self.last_request)))
        try:
            response = self.client.get('https://asharehub.com/v2/' + endpoint, params=params,
                                       headers={'X-API-Key': self.token}, follow_redirects=False)
            response.raise_for_status()
            if len(response.content) > 24 * 1024 * 1024:
                raise ValueError()
            rows = response.json()
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ValueError()
            LOG.info('Dividend source endpoint=%s rows=%s date=%s', endpoint, len(rows), self.day)
            return rows
        except Exception as exc:
            self.check_cancel()
            LOG.warning('Dividend source failed endpoint=%s error_type=%s', endpoint, type(exc).__name__)
            raise DividendSourceError('AShareHub 数据读取失败，保留原结果。') from None
        finally:
            self.last_request = time.monotonic()

    @staticmethod
    def mapped(rows):
        result = {}
        for row in rows:
            symbol = row.get('symbol')
            if not isinstance(symbol, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', symbol) or symbol in result:
                raise DividendSourceError('证券数据重复或代码无效。')
            result[symbol] = row
        return result

    def suspension_events(self):
        # Do not cache mutable current-day status or treat a failed empty page as no suspensions.
        rows, expected, pages = [], None, None
        for page in range(1, 100):
            data = self._get(DATACENTER, dict(reportName='RPT_CUSTOM_SUSPEND_DATA_INTERFACE', columns='ALL',
                filter=f'(MARKET="全部")(DATETIME=\'{self.day}\')', pageSize=500, pageNumber=page))
            result = data.get('result')
            if not data.get('success') or not isinstance(result, dict):
                raise DividendSourceError('停复牌清单无法读取，保留原结果。')
            if page == 1:
                expected, pages = result.get('count'), result.get('pages')
                if type(expected) is not int or type(pages) is not int or not 1 <= pages < 100:
                    raise DividendSourceError('停复牌清单分页无效。')
            if result.get('count') != expected or result.get('pages') != pages or not isinstance(result.get('data'), list):
                raise DividendSourceError('停复牌清单分页变化。')
            rows.extend(result['data'])
            if page == pages:
                break
        if len(rows) != expected:
            raise DividendSourceError('停复牌清单不完整。')
        return rows

    def proposal_rows(self, filter_text, *, report='RPT_F10_DIVIDEND_MAIN', date_field='REPORT_TIME', columns='ALL'):
        rows, seen, expected = [], set(), None
        page = 1
        while True:
            self.check_cancel()
            data = self._get('https://datacenter.eastmoney.com/securities/api/data/v1/get', dict(
                reportName=report, columns=columns, filter=filter_text,
                pageNumber=page, pageSize=500, sortColumns='SECUCODE,' + date_field,
                sortTypes='1,1', source='WEB', client='WEB'))
            result = data.get('result')
            if data.get('success') is not True or not isinstance(result, dict):
                raise DividendSourceError('分红方案报表不可用，保留原结果。')
            total, pages, batch = result.get('count'), result.get('pages'), result.get('data')
            if (type(total) is not int or type(pages) is not int or total <= 0
                    or pages != (total + 499) // 500 or pages > 500 or not isinstance(batch, list)
                    or len(batch) != min(500, total - (page - 1) * 500)):
                raise DividendSourceError('分红方案分页不完整。')
            if expected is None:
                expected = total
            if total != expected:
                raise DividendSourceError('分红方案采集期间覆盖变化。')
            for row in batch:
                if not isinstance(row, dict):
                    raise DividendSourceError('分红方案格式无效。')
                key = (row.get('SECUCODE'), row.get(date_field))
                if not all(key) or key in seen:
                    raise DividendSourceError('同报告期多条分红方案，无法确认版本。')
                seen.add(key)
                rows.append(row)
            if page == pages:
                break
            page += 1
        if len(rows) != expected:
            raise DividendSourceError('分红方案数量不完整。')
        return rows

    def fiscal_cash(self, symbols):
        from app.services.dividend_proposals import eastmoney_proposals, restate_current_share_basis, ProposalError
        rows = []
        for offset in range(0, len(symbols), 100):
            self.check_cancel()
            batch = set(symbols[offset:offset + 100])
            codes = [symbol.split('.')[0] for symbol in sorted(batch)]
            if any(not re.fullmatch(r'\d{6}', code) for code in codes):
                raise DividendSourceError('证券代码无效。')
            filter_text = "(REPORT_TIME>='{}-01-01')(SECURITY_CODE in ({}))".format(
                self.day.year - 4, ','.join('"' + code + '"' for code in codes))
            # Latest proposals change during a day; only validated outputs may
            # enter a published snapshot. No pre-validation permanent cache.
            values = self.proposal_rows(filter_text)
            if any(row.get('SECUCODE') not in batch for row in values):
                raise DividendSourceError('分红方案代码越界。')
            rows.extend(values)
        try:
            result = restate_current_share_basis(eastmoney_proposals(rows, self.day), rows, self.day)

        except ProposalError as exc:
            raise DividendSourceError(str(exc)) from None
        if set(result) != set(symbols):
            raise DividendSourceError('部分证券年报方案尚无法核验，保留原结果：' + ','.join(sorted(set(symbols)-set(result))[:10]))
        self.verify_share_events(result)
        return result

    def verify_share_events(self, proposals):
        from app.services.dividend_proposals import verify_equity_events, ProposalError
        proposals = {symbol: item for symbol, item in proposals.items()
                     if item['annual_dps'].get(item['fiscal_year']) != Decimal(0)}
        symbols = sorted(proposals)
        rows = []
        for offset in range(0, len(symbols), 100):
            batch = symbols[offset:offset + 100]
            values = self.proposal_rows('(SECUCODE in ({}))'.format(','.join('"'+symbol+'"' for symbol in batch)),
                report='RPT_F10_EH_EQUITY', date_field='END_DATE',
                columns='SECUCODE,SECURITY_CODE,END_DATE,CHANGE_REASON,TOTAL_SHARES,NOTICE_DATE')
            if any(row.get('SECUCODE') not in batch for row in values):
                raise DividendSourceError('权益变动证券代码越界。')
            rows.extend(values)
        try:
            verify_equity_events(proposals, rows, self.day, getattr(self, 'listed_dates', {}))
        except ProposalError as exc:
            raise DividendSourceError(str(exc)) from None

    def collect(self):
        moment = datetime.now(CHINA)
        if self.day != moment.date() or moment.time() < daytime(16):
            raise DividendSourceError('公开筛选仅支持当日收盘后；历史请使用聚宽恢复。')
        all_rows = self.mapped(self.ash('reference/stocks', {'list_status': 'L'}))
        if not all_rows or any(row.get('list_status') != 'L' for row in all_rows.values()):
            raise DividendSourceError('当前上市股票池未完整核验。')
        independent = self._selector_universe()
        census = {f"{r['code']}.{r['exchange']}": r for r in independent}
        if len(census) != len(independent) or set(census) != set(all_rows):
            raise DividendSourceError('股票池跨来源覆盖不一致。')
        eligible = []
        self.listed_dates = {}
        for symbol, row in all_rows.items():
            listed = row.get('list_date', '')
            if not isinstance(listed, str) or not re.fullmatch(r'\d{8}', listed):
                raise DividendSourceError('上市日期缺失。')
            listed = f'{listed[:4]}-{listed[4:6]}-{listed[6:]}'
            self.listed_dates[symbol] = listed
            if listed != census[symbol].get('list_date'):
                raise DividendSourceError('上市日期跨来源不一致：' + symbol)
            if not mature(listed, self.day) or symbol.startswith(('4', '8')):
                continue
            if not isinstance(row.get('name'), str) or not row['name']:
                raise DividendSourceError('证券名称缺失。')
            if census[symbol].get('name') != row['name']:
                raise DividendSourceError('证券名称跨来源不一致，无法核验当前ST状态：' + symbol)
            if any(tag in row['name'] for tag in ('ST', '*', '退')):
                continue
            eligible.append(row)
        quotes = self.mapped(self.ash('market/realtime', {}))
        events = self.suspension_events()
        active = []
        for row in eligible:
            quote = quotes.get(row['symbol'])
            if not suspended(row['symbol'], quote, events, self.day):
                quote_name = quote.get('name')
                if not isinstance(quote_name, str) or not quote_name:
                    raise DividendSourceError('行情证券名称缺失：' + row['symbol'])
                # XD/XR/DR or ordinary short-name variations are not ST changes.
                if any(tag in quote_name for tag in ('ST', '*', '退')):
                    raise DividendSourceError('当前ST/退市标识跨接口不一致：' + row['symbol'])
                active.append(row)
        valuations = self.mapped(self.ash('market/fundamentals', {'trade_date': self.day.strftime('%Y%m%d')}))
        for row in active:
            val = valuations.get(row['symbol'])
            if val is None or val.get('trade_date') != self.day.strftime('%Y%m%d'):
                raise DividendSourceError('非停牌股票估值缺失或日期未更新：' + row['symbol'])
            if decimal(val['close'], positive=True) != decimal(quotes[row['symbol']]['price'], positive=True):
                raise DividendSourceError('收盘价跨接口不一致：' + row['symbol'])
            decimal(val['total_mv'], positive=True)
        if any('pe_ttm' not in valuations[r['symbol']] for r in active):
            raise DividendSourceError('PE 字段缺失。')
        candidates = [r for r in active if valuations[r['symbol']]['pe_ttm'] is not None
                      and 0 < decimal(valuations[r['symbol']]['pe_ttm']) < 12]
        cash = self.fiscal_cash([r['symbol'] for r in candidates])
        industry_rows = self.ash('reference/industries', {})
        # The full industry feed also contains legacy/non-equity identifiers
        # (live sample T00018.SH). Only join to the independently verified pool;
        # duplicates and missing classification for qualifying stocks still fail.
        industries = self.mapped([row for row in industry_rows if row.get('symbol') in all_rows])
        meta, records = rank_records(active, valuations, cash, industries, self.day, len(all_rows))
        self.enrich_history(records)
        return meta, records

    def enrich_history(self, records):
        from app.services.dividend_history import enrich_history
        enrich_history(records, client=self.client, settings=self.settings, as_of=self.day.isoformat(),
                       cache_dir=self.cache_dir, listed_dates=self.listed_dates, check_cancel=self.check_cancel)
