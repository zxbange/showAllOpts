"""Pure local strategy calculations. No JoinQuant imports or source database writes."""
import bisect
import builtins
import datetime
import json
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from statistics import median
from types import SimpleNamespace
import pandas as pd
from sqlalchemy import Numeric
from app.models.earnings import EarningsStock

dt = datetime

def as_date(value):
    return pd.Timestamp(value).date()

def _progress_note(*args):
    pass

def _progress_items(values, *args):
    return values

def _momentum_rules(settings, data, stamp):
    china_now = lambda: stamp
    INDUSTRY_TYPE = settings['industry_type']

    def calculate_momentum(trade_date, industry_type=INDUSTRY_TYPE):
        """计算一个交易日的全部行业；仅获取数据，不连接数据库。"""
        _progress_note('获取行业成分及行情，计算动量')
        day = as_date(trade_date)
        date_text = str(day)
        industries, members, prices, window = data['industries'], data['members'], data['prices'].copy(), data['window']
        members = {c:set(values) for c,values in members.items()}
        prices['price_date'] = pd.to_datetime(prices['time']).dt.date
        front_stocks = set()
        valid_count = 0
        for code, group in prices.groupby('code'):
            if group['price_date'].duplicated().any():
                raise ValueError('{} {} 行情日期重复。'.format(date_text, code))
            close = group.set_index('price_date')['close']
            if window[0] not in close.index or day not in close.index:
                continue
            start_close, end_close = close.loc[window[0]], close.loc[day]
            if pd.isnull(start_close) or pd.isnull(end_close):
                continue
            start_close, end_close = float(start_close), float(end_close)
            if not (math.isfinite(start_close) and math.isfinite(end_close)):
                continue
            if start_close <= 0 or end_close <= 0:
                continue
            valid_count += 1
            if (end_close - start_close) / start_close > 0.2:
                front_stocks.add(code)
        if not valid_count:
            raise ValueError('{} 没有可计算涨幅的股票，停止写入。'.format(date_text))

        records = []
        for code in industries.index:
            total = len(members[code])
            count = len(members[code] & front_stocks)
            score = count * count / total if total else 0.0
            records.append({
                'trade_date': day,
                'industry_type': industry_type,
                'industry_code': str(code),
                'industry_name': str(industries.loc[code, 'name']),
                'momentum': Decimal(format(score, '.2f')),
                'front_stock_count': count,
                'industry_stock_count': total,
            })
        # 与原程序相同：先按两位小数的得分降序，再按行业代码降序。
        records.sort(key=lambda r: (r['momentum'], r['industry_code']), reverse=True)
        for rank, record in enumerate(records, 1):
            record['momentum_rank'] = rank
        return records
    return SimpleNamespace(**locals())


def _crowding_rules(settings, data, stamp):
    china_now = lambda: stamp
    TOP_PCT = settings['top_pct']

    def summarize_money(pairs, top_pct=TOP_PCT):
        """纯计算：零成交额股票计入总数量；无效/负数/重复数据拒绝入库。"""
        pct = Decimal(str(top_pct))
        if not pct.is_finite() or not Decimal('0') < pct <= Decimal('1'):
            raise ValueError('TOP_PCT 必须在 (0, 1] 内。')
        if pct != pct.quantize(Decimal('0.000001')):
            raise ValueError('TOP_PCT 最多支持六位小数。')
        values = []
        codes = set()
        for code, money in pairs:
            if code in codes:
                raise ValueError('单日行情存在重复股票，停止写入。')
            codes.add(code)
            try:
                amount = Decimal(str(money))
            except (InvalidOperation, ValueError):
                raise ValueError('成交额不是有效数值，停止写入。') from None
            if not amount.is_finite() or amount < 0:
                raise ValueError('成交额缺失、非有限或为负数，停止写入。')
            values.append(amount)
        if len(values) < 100:
            raise ValueError('有效股票不足 100 只，停止写入。')
        count = builtins.max(int(Decimal(len(values)) * pct), 1)
        total = builtins.sum(values, Decimal('0'))
        if total <= 0:
            raise ValueError('全市场成交额为零，停止写入。')
        top = builtins.sum(sorted(values, reverse=True)[:count], Decimal('0'))
        return dict(top_pct=pct, ratio=(top / total).quantize(Decimal('0.0000000001')),
                    top_money=top.quantize(Decimal('0.01')),
                    total_money=total.quantize(Decimal('0.01')),
                    stock_count=len(values), top_stock_count=count)
    return SimpleNamespace(**locals())


def _microcap_rules(settings, data, stamp):
    china_now = lambda: stamp
    MICRO_NUM = settings['micro_num']

    RAW_FIELDS = ('micro_median', 'relative_ratio', 'crowding_ratio', 'amount_crowding_ratio')

    PCT_FIELDS = ('cap_pct', 'relative_pct', 'crowding_pct', 'amount_crowding_pct')

    Q = Decimal('0.0000000001')

    def number(value):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None

    def rounded(value):
        return Decimal(value).quantize(Q)

    def series_key(start, micro_num, sample_step):
        return 'micro{}_{}d_{}_v1'.format(micro_num, sample_step, as_date(start).strftime('%Y%m%d'))

    def summarize_universe(rows, turnovers, amounts, micro_num):
        valid = []
        for code, row in rows.items():
            cap, pe, pb = [number(row.get(field)) for field in ('market_cap', 'pe_ratio', 'pb_ratio')]
            today_turnover = number(row.get('turnover_ratio'))
            history = [value for value in turnovers.get(code, []) if value is not None and value >= 0]
            money = amounts.get(code)
            # 沿用原程序 dropna 口径：当期估值、当日换手率、成交额及过去换手率均须有值。
            if cap is None or cap <= 0 or pe is None or pb is None or today_turnover is None:
                continue
            if today_turnover < 0 or not history or money is None or money < 0:
                continue
            valid.append(dict(code=code, market_cap=cap, pe=pe, pb=pb, money=money,
                              turnover=builtins.sum(history) / len(history)))
        if len(valid) < micro_num:
            raise ValueError('完整有效股票不足 {} 只，停止写入。'.format(micro_num))
        valid.sort(key=lambda row: (row['market_cap'], row['code']))
        micro = valid[:micro_num]
        micro_median = median([row['market_cap'] for row in micro])
        all_median = median([row['market_cap'] for row in valid])
        all_turnover = builtins.sum(row['turnover'] for row in valid) / len(valid)
        micro_turnover = builtins.sum(row['turnover'] for row in micro) / micro_num
        total_money = builtins.sum(row['money'] for row in valid)
        if all_turnover <= 0 or total_money <= 0:
            raise ValueError('市场换手率或成交额分母为零，停止写入。')
        metrics = dict(
            universe_count=len(valid), micro_median=rounded(micro_median), all_median=rounded(all_median),
            relative_ratio=rounded(micro_median / all_median),
            crowding_ratio=rounded(micro_turnover / all_turnover),
            amount_crowding_ratio=rounded(builtins.sum(row['money'] for row in micro) / total_money),
            pe_median=rounded(median([row['pe'] for row in micro])),
            pb_median=rounded(median([row['pb'] for row in micro])),
        )
        return metrics, [row['code'] for row in micro]

    def append_percentiles(record, histories):
        # pandas rank(pct=True, method='average')，包括当前值；相等值取平均名次。
        for raw_field, pct_field in zip(RAW_FIELDS, PCT_FIELDS):
            # 先统一到数据库精度，避免一次性计算与从数据库续算的并列排名不同。
            value = rounded(record[raw_field])
            record[raw_field] = value
            history = histories[raw_field]
            left, right = bisect.bisect_left(history, value), bisect.bisect_right(history, value)
            record[pct_field] = rounded(Decimal(left + right + 2) / (2 * (len(history) + 1)))
            bisect.insort(history, value)
        record['score'] = rounded(record['relative_pct'] * Decimal('0.50')
                                  + record['crowding_pct'] * Decimal('0.25')
                                  + record['amount_crowding_pct'] * Decimal('0.25'))
        record['sample_count'] = len(histories[RAW_FIELDS[0]])
    return SimpleNamespace(**locals())


def _breadth_rules(settings, data, stamp):
    china_now = lambda: stamp
    PERIODS = (5, 10, 20, 30)

    def series_key(micro_num):
        return 'micro{}_hl_v1'.format(micro_num)

    def high_low_counts(closes):
        if len(closes) != 31 or builtins.any(not math.isfinite(float(p)) or p <= 0 for p in closes):
            raise ValueError('新高新低需要连续 31 个有效交易日收盘价。')
        current = closes[-1]
        return {key: int(hit) for period in PERIODS for key, hit in (
            ('high_{}'.format(period), current >= builtins.max(closes[-period-1:-1])),
            ('low_{}'.format(period), current <= builtins.min(closes[-period-1:-1])),
        )}

    def count_prices(frame, codes, calendar):
        if frame is None or not {'code', 'time', 'close'}.issubset(frame.columns):
            raise ValueError('历史行情字段缺失。')
        rows = frame.to_dict('records')
        if len(rows) != len(codes)*len(calendar):
            raise ValueError('历史行情未完整返回，拒绝将缺失记录按零统计。')
        prices = {code: {} for code in codes}
        for row in rows:
            code, day = str(row['code']), as_date(row['time'])
            if code not in prices or day not in calendar or day in prices[code]:
                raise ValueError('行情包含重复、越界日期或股票。')
            try:
                price = float(row['close'])
            except (TypeError, ValueError):
                raise ValueError('收盘价无效。') from None
            if not math.isfinite(price) or price <= 0:
                raise ValueError('收盘价缺失或非正数。')
            prices[code][day] = price
        counts = {'{}_{}'.format(side, period): 0 for period in PERIODS for side in ('high', 'low')}
        for code in codes:
            if set(prices[code]) != set(calendar):
                raise ValueError('个股历史行情缺失。')
            for key, value in high_low_counts([prices[code][day] for day in calendar]).items():
                counts[key] += value
        return counts
    return SimpleNamespace(**locals())


def _dividend_rules(settings, data, stamp):
    china_now = lambda: stamp
    RULE_VERSION = 'dividend_top25_v2'

    METHODS = ('classic', 'value')

    def number(value):
        try:
            result = Decimal(str(value))
            return result if result.is_finite() else None
        except (InvalidOperation, ValueError, TypeError):
            return None

    def rounded(value):
        value = number(value)
        if value is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 40
            return value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)

    def optional_date(value):
        return None if value is None or pd.isna(value) else as_date(value)

    def select(caps, cash, fundamentals):
        # 比较使用未舍入值；相同分红比例按代码排序，结果稳定。
        ratios = {}
        for code, cap in caps.items():
            item = cash.get(code, {})
            market_cap = number(cap['market_cap'])
            if market_cap is None or market_cap <= 0:
                raise ValueError('市值无效。')
            ratios[code] = (item.get('cash_1y', Decimal(0))/100/market_cap,
                            item.get('cash_3y', Decimal(0))/100/market_cap)
        dividend_codes = sorted((c for c in caps if cash.get(c, {}).get('has_3y')),
                                key=lambda c: (-ratios[c][1], c))
        classic = []
        for code in dividend_codes[:len(dividend_codes)//10]:
            pe = number(caps[code]['pe_ratio'])
            metrics = fundamentals.get(code, {})
            revenue, profit, roe = [number(metrics.get(f)) for f in
                                   ('inc_total_revenue_year_on_year', 'inc_net_profit_year_on_year', 'inc_return')]
            if None not in (pe, revenue, profit, roe) and revenue > 3 and profit > 8 and roe > Decimal('4.5'):
                if Decimal('.1') < pe/profit < 2:
                    classic.append(code)
        value = sorted((c for c in caps if number(caps[c]['pe_ratio']) is not None
                        and 0 < number(caps[c]['pe_ratio']) < 8 and ratios[c][0] > 4),
                       key=lambda c: (-ratios[c][0], c))
        return dict(classic=classic[:25], value=value[:25]), ratios, dict(classic=len(classic), value=len(value))

    def calculate(day):
        names, quotes, caps = data['names'], data['quotes'], data['caps']
        cash = {}
        for row in data['distributions']:
            code = row['code']
            registration, ex = optional_date(row['a_registration_date']), optional_date(row['a_xr_date'])
            amount = Decimal(0) if pd.isna(row['bonus_amount_rmb']) else number(row['bonus_amount_rmb'])
            if amount is None or amount < 0:
                raise ValueError('派现金额无效')
            item = cash.setdefault(code, dict(cash_1y=Decimal(0), cash_3y=Decimal(0), has_3y=False))
            if registration and day-datetime.timedelta(days=1095) <= registration <= day:
                item['cash_3y'] += amount
                item['has_3y'] = True
            if ex and day-datetime.timedelta(days=365) < ex <= day:
                item['cash_1y'] += amount
        fundamentals = data['fundamentals']
        selected, ratios, counts = select(caps, cash, fundamentals)
        chosen = sorted(set(selected['classic']+selected['value']))
        _progress_note('补充细分行业并生成两组 Top25')
        industries = data['industries']
        if not isinstance(industries, dict) or set(chosen)-set(industries):
            raise ValueError('入选股票行业查询未完整返回。')
        records = []
        for method in METHODS:
            for rank, code in enumerate(selected[method], 1):
                info = industries.get(code) or {}
                level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if info.get(k)), None)
                industry = info.get(level, {})
                cap, fin, payout = caps[code], fundamentals.get(code, {}), cash.get(code, {})
                profit = number(fin.get('inc_net_profit_year_on_year'))
                pe = number(cap['pe_ratio'])
                records.append(dict(method=method, code=code, trade_date=day, rank=rank, name=names[code],
                    industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'), industry_level=level,
                    dividend_yield_1y=rounded(ratios[code][0]), dividend_ratio_3y=rounded(ratios[code][1]),
                    dividend_cash_1y=rounded(payout.get('cash_1y', 0)), dividend_cash_3y=rounded(payout.get('cash_3y', 0)),
                    pe_ttm=rounded(pe), pb=rounded(cap['pb_ratio']), market_cap=rounded(cap['market_cap']), close=rounded(quotes[code]),
                    revenue_growth=rounded(fin.get('inc_total_revenue_year_on_year')), profit_growth=rounded(profit),
                    adjusted_roe=rounded(fin.get('inc_return')), peg=rounded(pe/profit) if profit and profit > 0 else None,
                    report_period=optional_date(fin.get('statDate')), publication_date=optional_date(fin.get('pubDate'))))
        meta = dict(trade_date=day, rule_version=RULE_VERSION, universe_count=len(names), eligible_count=len(caps),
                    classic_candidates=counts['classic'], value_candidates=counts['value'],
                    classic_count=len(selected['classic']), value_count=len(selected['value']))
        return meta, records
    return SimpleNamespace(**locals())


def _growth_rules(settings, data, stamp):
    china_now = lambda: stamp
    RULE_VERSION = 'growth_cumulative_parent_eps_v1'

    GROUPS = {'annual': (12, 31), 'q1': (3, 31), 'q2': (6, 30), 'q3': (9, 30)}

    def number(value):
        try:
            result = Decimal(str(value))
            return result if result.is_finite() else None
        except (InvalidOperation, ValueError, TypeError):
            return None

    def rounded(value, places=6):
        value = number(value)
        if value is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 50
            return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)

    def latest_reports(source, as_of):
        # 先选最新三期再检查质量，不可用旧的合格报表顶替最新不合格报表。
        unique = {}
        for raw in source:
            period, pub = as_date(raw['report_period']), as_date(raw['publication_date'])
            if (period.month, period.day) not in GROUPS.values() or period > as_of or pub > as_of:
                raise ValueError('财报报告期或公告日期无效。')
            if pub < period:
                raise ValueError('财报公告早于报告期末。')
            row = dict(raw, report_period=period, publication_date=pub)
            old = unique.get(period)
            if old is None or pub > old['publication_date']:
                unique[period] = row
            elif pub == old['publication_date'] and builtins.any(number(row[k]) != number(old[k]) for k in ('net_profit', 'eps')):
                raise ValueError('相同报告期和公告日期存在冲突财务数据。')
        selected = {}
        for name, month_day in GROUPS.items():
            group = sorted((r for d, r in unique.items() if (d.month, d.day) == month_day), key=lambda r: r['report_period'])[-3:]
            if len(group) != 3:
                return None
            years = [r['report_period'].year for r in group]
            if years != list(range(years[0], years[0]+3)):
                return None  # 同比必须比较连续年份的同类报告。
            if builtins.any(number(r[k]) is None for r in group for k in ('net_profit', 'eps')):
                return None
            selected[name] = group
        return selected

    def evaluate(groups):
        if groups is None:
            return None
        evidence = {}
        annual_growth = None
        for name, group in groups.items():
            if builtins.any(number(group[-1][key]) <= 0 for key in ('net_profit', 'eps')):
                return None
            output = []
            for i, row in enumerate(group):
                record = {key: str(row[key]) for key in ('report_period', 'publication_date')}
                for field, target in (('net_profit', 'profit_growth'), ('eps', 'eps_growth')):
                    value = number(row[field])
                    record[field] = str(value)
                    record[target] = None
                    if i:
                        previous = number(group[i-1][field])
                        if previous == 0:
                            return None
                        # 先用原值交叉相乘判断，避免把 19.999...% 四舍五入成达标。
                        if (value-previous)*100 < builtins.abs(previous)*20:
                            return None
                        rate = (value-previous)/builtins.abs(previous)*100
                        record[target] = str(rounded(rate, 8))
                        if name == 'annual' and i == 2 and field == 'net_profit':
                            annual_growth = rate
                output.append(record)
            evidence[name] = output
        return annual_growth, evidence

    def select(all_reports, as_of):
        chosen, evaluated = [], 0
        for code in _progress_items(sorted(all_reports), '检查四类财报'):
            groups = latest_reports(all_reports[code], as_of)
            if groups is not None:
                evaluated += 1
                result = evaluate(groups)
                if result is not None:
                    chosen.append((code, result[0], result[1]))
        chosen.sort(key=lambda item: (-item[1], item[0]))
        return chosen, evaluated

    def calculate(as_of, valuation_day):
        names, all_reports = data['names'], data['reports']
        chosen, evaluated = select(all_reports, as_of)
        if evaluated == 0:
            raise ValueError('没有任何股票取得完整四类三期财报，检查数据权限及字段；保留原快照。')
        caps, industries = data['caps'], data['industries']
        records = []
        for rank, (code, growth, reports) in enumerate(chosen, 1):
            info = industries.get(code) or {}
            level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if info.get(k)), None)
            industry = info.get(level, {})
            cap = caps.get(code, {})
            records.append(dict(code=code, as_of_date=as_of, rank=rank, name=names[code],
                industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'), industry_level=level,
                pe_ttm=rounded(cap.get('pe_ratio')), pb=rounded(cap.get('pb_ratio')), market_cap=rounded(cap.get('market_cap')),
                annual_profit_growth=rounded(growth, 8), reports=reports))
        meta = dict(as_of_date=as_of, valuation_date=valuation_day, rule_version=RULE_VERSION,
                    universe_count=len(names), evaluated_count=evaluated, matched_count=len(records))
        return meta, records
    return SimpleNamespace(**locals())


def _earnings_rules(settings, data, stamp):
    china_now = lambda: stamp
    RULE_VERSION = 'announcement-open-v1'

    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    def clean(value):
        # 原始字段用于审计；JSON 不允许 NaN/Infinity。
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if value is None or pd.isna(value):
            return None
        if isinstance(value, (dt.datetime, dt.date)):
            return value.isoformat()
        if isinstance(value, (str, bool)):
            return value
        return number(value)

    def checked_rows(frame, fields, label):
        if frame is None or not set(fields).issubset(frame.columns):
            raise ValueError('{} 返回缺失或字段不完整，取消本次入库。'.format(label))
        return frame.to_dict('records')

    def price_signal(previous_close, open_price, close_price):
        values = [number(v) for v in (previous_close, open_price, close_price)]
        if builtins.any(v is None or v <= 0 for v in values):
            return None
        previous, opening, closing = [Decimal(str(v)) for v in values]
        # 精确等于 3% 不通过，避免二进制浮点边界误差。
        if opening <= previous * Decimal('1.03') or closing < opening:
            return None
        return dict(open_gap_pct=float((opening / previous - 1) * 100),
                    intraday_pct=float((closing / opening - 1) * 100))

    def database_record(record):
        """按表定义量化全部 DECIMAL 列，保留 Decimal 交给 MySQL 驱动。"""
        result = dict(record)
        for column in EarningsStock.__table__.columns:
            if not isinstance(column.type, Numeric):
                continue
            value = record.get(column.name)
            if value is None:
                if not column.nullable:
                    raise ValueError('{} 不允许为空。'.format(column.name))
                result[column.name] = None
                continue
            precision, scale = column.type.precision, column.type.scale
            try:
                decimal_value = Decimal(str(value))
                limit = Decimal(10) ** (precision - scale)
                if not decimal_value.is_finite() or decimal_value.copy_abs() >= limit:
                    raise ValueError('{} 不是有限值或超出字段范围。'.format(column.name))
                with localcontext() as context:
                    context.prec = precision + 2
                    rounded = decimal_value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
                if rounded.copy_abs() >= limit:
                    raise ValueError('{} 四舍五入后超出字段范围。'.format(column.name))
            except (InvalidOperation, TypeError):
                raise ValueError('{} 无法转换为数据库定点数。'.format(column.name)) from None
            result[column.name] = rounded
        return result

    def screen(codes, names, events, period, as_of):
        grouped = {}
        for e in events:
            if e['report_period'] != str(period) or as_date(e['publication_date']) > as_of or e['code'] not in names:
                raise ValueError('公告报告期、股票代码或截止日期异常。')
            grouped.setdefault(e['code'], []).append(e)
        results = []
        for index, (code, disclosures) in enumerate(_progress_items(sorted(grouped.items()), '股票筛选', lambda item: item[0]), 1):
            qualifying = [e for e in disclosures if e['eligible']]
            if not qualifying:
                continue
            first = builtins.min(as_date(e['publication_date']) for e in qualifying)
            calendar = data['calendars'][code]
            frame = data['prices'][code]
            checked_rows(frame, ['open', 'close', 'high', 'low', 'paused', 'volume'], '股票行情')
            bars = {as_date(day): row.to_dict() for day, row in frame.iterrows()}
            hits = []
            for e in qualifying:
                pub = as_date(e['publication_date'])
                # 原策略的公告日行情；非交易日公告对应下一个交易日，不倒取公告前行情。
                day = next((d for d in calendar if d >= pub), None)
                if day is None:
                    continue
                position = calendar.index(day)
                if position == 0:
                    continue
                previous = calendar[position-1]
                current, prior = bars.get(day), bars.get(previous)
                if current is None or prior is None:
                    raise ValueError('{} 行情缺失，取消本次入库。'.format(code))
                if number(current['paused']) is None:
                    raise ValueError('{} 停牌状态缺失。'.format(code))
                if number(current['volume']) is None:
                    raise ValueError('{} 成交量缺失。'.format(code))
                if current['paused'] or number(current['volume']) == 0:
                    continue
                if builtins.any(number(current[k]) is None or number(current[k]) <= 0 for k in ('open', 'close', 'high', 'low')):
                    raise ValueError('{} 已成交日的价格不完整。'.format(code))
                # 上市前缺失前收盘，不构造跳空信号；不延迟到复牌日追认。
                signal = price_signal(prior['close'], current['open'], current['close'])
                if signal:
                    signal['date'] = str(day)
                    e['signal'] = signal
                    hits.append(e)
            if not hits:
                continue
            selected = sorted(hits, key=lambda e: (e['signal']['date'], e['signal']['open_gap_pct'], e['kind']))[-1]
            industry_data = data['industries']
            if not isinstance(industry_data, dict) or code not in industry_data:
                raise ValueError('{} 行业接口返回异常。'.format(code))
            industries = industry_data[code]
            level = next((k for k in ('sw_l3', 'sw_l2', 'sw_l1') if industries.get(k)), None)
            industry = industries.get(level, {})
            signal_day = as_date(selected['signal']['date'])
            offset = calendar.index(signal_day)
            chart_days = calendar[builtins.max(0, offset-10):offset+21]
            prices = [dict(date=str(d), **{k: number(bars[d][k]) for k in ('open', 'close', 'high', 'low')})
                      for d in chart_days if d in bars]
            record = dict(report_period=period, code=code, name=names[code],
                          industry_code=industry.get('industry_code'), industry_name=industry.get('industry_name'),
                          industry_level=level, signal_date=signal_day,
                          announcement_date=as_date(selected['publication_date']), signal_source=selected['kind'],
                          profit_basis=selected['profit_basis'], profit_yoy=selected['profit_yoy'],
                          net_profit=selected['net_profit'], open_gap_pct=selected['signal']['open_gap_pct'],
                          intraday_pct=selected['signal']['intraday_pct'], valuation_date=as_of,
                          events=disclosures, prices=prices)
            results.append(record)
        valuations = data['caps']
        for r in results:
            v = valuations.get(r['code'], {})
            for target, source in [('market_cap', 'market_cap'), ('circulating_market_cap', 'circulating_market_cap'),
                                   ('pe_ttm', 'pe_ratio'), ('pb', 'pb_ratio')]:
                r[target] = number(v.get(source))
        return results
    return SimpleNamespace(**locals())
