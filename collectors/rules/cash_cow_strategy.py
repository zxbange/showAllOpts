"""奶牛股聚宽适配器；由 sync_cash_cow.py 内嵌到两个可独立运行的入口。"""

def _cash_cow_strategy(_apis, _config):
    settings = strategy_settings('奶牛股')
    Base = declarative_base()
    RULE_VERSION = 'cash_cow_static_display_v2'
    get_price = _apis.get('get_price')

    class CashCowSnapshot(Base):
        __tablename__ = 'cash_cow_snapshot'
        __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'}
        id = Column(Integer, primary_key=True)
        as_of_date = Column(Date, nullable=False)
        updated_at = Column(DateTime, nullable=False)
        payload = Column(JSON, nullable=False)

    # BEGIN CASH COW RULES
    # END CASH COW RULES

    class JoinQuantSource:
        def __init__(self, day, batch_size):
            self.day, self.as_of, self.batch_size = day, day.isoformat(), batch_size
            self.codes, self.events, self.capital, self.daily = {}, {}, {}, {}
            self.finance, self.query = _apis['finance'], _apis['query']

        def universe(self):
            frame = _apis['get_all_securities'](types=['stock'], date=self.day)
            if frame is None or frame.empty or 'display_name' not in frame.columns:
                raise ValueError('聚宽股票池缺失，保留已有结果。')
            result = []
            for full_code, row in frame.iterrows():
                code = str(full_code).split('.')[0]
                if not re.fullmatch(r'\d{6}', code) or code in self.codes:
                    raise ValueError('股票池包含无效或重复代码。')
                self.codes[code] = str(full_code)
                exchange = 'SH' if code.startswith('6') else 'BJ' if code.startswith(('4', '8', '92')) else 'SZ'
                result.append(dict(code=code, name=str(row['display_name']), exchange=exchange))
            return result

        def read(self, table, fields, conditions):
            selection = self.query(*[getattr(table, key) for key in fields]).filter(*conditions)
            fetch = lambda cursor: self.finance.run_query(selection.filter(table.id > cursor).order_by(table.id).limit(2000))
            for page in _source_pages(fetch, fields=fields, label='奶牛股公开报表', page_size=2000):
                for row in page.to_dict('records'):
                    yield row

        def dividend_rows(self, codes, start_year=None, end_year=None):
            table = self.finance.STK_XR_XD
            fields = ('id', 'code', 'report_date', 'implementation_pub_date', 'a_bonus_date', 'a_xr_date',
                      'bonus_amount_rmb', 'bonus_ratio_rmb', 'dividend_ratio', 'transfer_ratio',
                      'implementation_bonusnote', 'bonus_cancel_pub_date')
            for offset in range(0, len(codes), self.batch_size):
                batch = codes[offset:offset + self.batch_size]
                conditions = [table.code.in_([self.codes[c] for c in batch]),
                              table.implementation_pub_date <= self.day]
                if start_year is not None:
                    conditions += [table.report_date >= datetime.date(start_year, 1, 1),
                                   table.report_date <= datetime.date(end_year, 12, 31)]
                for row in self.read(table, fields, conditions):
                    code = str(row['code']).split('.')[0]
                    if code not in batch:
                        raise ValueError('分红记录包含范围外股票。')
                    row['paid'] = _optional_date(row['a_bonus_date']) or _optional_date(row['a_xr_date'])
                    cancelled = _optional_date(row['bonus_cancel_pub_date'])
                    if row['paid'] and row['paid'] <= self.day and not (cancelled and cancelled <= self.day):
                        yield row

        def dividends(self, year, codes):
            result, seen, invalid = {}, set(), set()
            for row in self.dividend_rows(codes, year, year):
                code = str(row['code']).split('.')[0]
                fiscal = _optional_date(row['report_date'])
                key = (code, fiscal, row['paid'])
                value = _float_number(row['bonus_amount_rmb'])
                if fiscal is None or fiscal.year != year or key in seen or value is None or value < 0:
                    invalid.add(code)
                else:
                    result[code] = result.get(code, 0.0) + value * 10000  # 万元 -> 元
                seen.add(key)
            return {code: dict(cash_dividend=None if code in invalid else result.get(code)) for code in codes}

        def financials(self, year, codes):
            result = {}
            tables = [('indicator', 'adjusted_profit'), ('income', 'np_parent_company_owners'),
                      ('cash_flow', 'net_operate_cash_flow')]
            for offset in range(0, len(codes), self.batch_size):
                batch = codes[offset:offset + self.batch_size]
                values = {c: {} for c in batch}
                # Separate tables preserve a missing statement as missing, not an inner-join disappearance.
                for name, field in tables:
                    table = _apis[name]
                    q = self.query(table.code, table.statDate, table.pubDate, getattr(table, field)).filter(
                        table.code.in_([self.codes[c] for c in batch]))
                    frame = _apis['get_fundamentals'](q, statDate=str(year))
                    rows = _checked_records(frame, ('code', 'statDate', 'pubDate', field), name)
                    seen = set()
                    for row in rows:
                        code = str(row['code']).split('.')[0]
                        if code not in batch or code in seen:
                            raise ValueError('年度财报代码重复或越界。')
                        seen.add(code)
                        published = _optional_date(row['pubDate'])
                        if (_optional_date(row['statDate']) == datetime.date(year, 12, 31)
                                and published is not None and published <= self.day):
                            values[code][field] = _float_number(row[field])
                for code, row in values.items():
                    result[code] = dict(adjusted_profit=row.get('adjusted_profit'),
                        net_profit=row.get('np_parent_company_owners'), operating_cashflow=row.get('net_operate_cash_flow'))
            return result

        def capital_stable(self, code, start_year):
            if code not in self.capital:
                t = self.finance.STK_CAPITAL_CHANGE
                self.capital[code] = sorted(self.read(t,
                    ('id', 'change_date', 'pub_date', 'share_total', 'change_reason'),
                    [t.code == self.codes[code], t.change_date <= self.day, t.pub_date <= self.day]),
                    key=lambda row: (str(row['change_date']), int(row['id'])))
            baseline = None
            for row in self.capital[code]:
                day, shares = _optional_date(row['change_date']), _float_number(row['share_total'])
                if day is None or shares is None or shares <= 0:
                    return False
                if day <= datetime.date(start_year, 1, 1):
                    baseline = shares
                    continue
                reason = str(row['change_reason'] or '')
                ordinary = reason in ('增发A股上市', '增发上市', '超额配售', '回购注销',
                                      '股份回购注销', '股权激励股份回购注销')
                if (baseline is None or (shares != baseline and not ordinary)
                        or re.search(r'拆|合股|缩股|配股|送股|转增', reason)):
                    return False
                baseline = shares
            return baseline is not None

        def per_share_dividends(self, code, start_year, end_year):
            if code not in self.events:
                self.events[code] = list(self.dividend_rows([code]))
            annual, seen, invalid, action = {}, set(), set(), False
            for row in self.events[code]:
                # A corporate action after the fiscal window also invalidates comparison with today's price.
                if row['paid'].year >= start_year:
                    note = str(row['implementation_bonusnote'] or '')
                    if (any((_float_number(row[f]) or 0) > 0 for f in ('dividend_ratio', 'transfer_ratio'))
                            or re.search(r'送\s*[1-9]|转增?\s*[1-9]|配股|拆股|合股', note)):
                        action = True
                fiscal = _optional_date(row['report_date'])
                if fiscal is None or not start_year <= fiscal.year <= end_year:
                    continue
                year, key = fiscal.year, (fiscal, row['paid'])
                rate = _float_number(row['bonus_ratio_rmb'])
                note = str(row['implementation_bonusnote'] or '')
                if key in seen or rate is None or rate < 0 or re.search(r'差异|部分股东|公众股|不参与', note):
                    invalid.add(year)
                else:
                    annual[year] = annual.get(year, 0.0) + rate / 10  # 元/10股 -> 元/股
                seen.add(key)
            if action or not self.capital_stable(code, start_year):
                return {year: None for year in range(start_year, end_year + 1)}
            return {year: None if year in invalid else annual.get(year) for year in range(start_year, end_year + 1)}

        def per_share_dividends_by_period(self, code, end_year, periods):
            return {p: self.per_share_dividends(code, end_year - p + 1, end_year) for p in periods}

        def prices(self, code, start_year, end_year):
            if code not in self.daily:
                frame = get_price(self.codes[code], start_date=datetime.date(start_year, 1, 1),
                    end_date=self.day, frequency='daily', fields=['close'], fq=None, skip_paused=True, panel=False)
                if frame is None or 'close' not in frame.columns:
                    raise ValueError('历史日线缺失。')
                daily = [(as_date(day), _float_number(row['close'])) for day, row in frame.iterrows()]
                days = [day for day, _ in daily]
                if days != sorted(set(days)) or any(not datetime.date(start_year, 1, 1) <= d <= self.day for d in days):
                    raise ValueError('日线日期重复、乱序或越界。')
                self.daily[code] = daily
            output = {}
            for year in range(start_year, end_year + 1):
                values = [(d, v) for d, v in self.daily[code] if d.year == year]
                valid = (values and values[0][0].month == 1 and values[-1][0].month == 12
                         and all(v is not None and v > 0 for _, v in values))
                output[year] = dict(year_average_close=sum(v for _, v in values) / len(values) if valid else None,
                    price_date=values[-1][0].isoformat() if values else None)
            return output

        def current_dividend_yield(self, code):
            year = self.day.year - 1
            annual = self.per_share_dividends(code, year - 2, year)
            daily = self.daily.get(code, [])
            day, close = daily[-1] if daily else (None, None)
            fresh = day is not None and (self.day - day).days <= 10 and close is not None and close > 0
            history = [dict(fiscal_year=y, yield_pct=100 * annual[y] / close
                            if annual.get(y) is not None and fresh else None) for y in range(year, year - 3, -1)]
            return dict(current_yield=history[0]['yield_pct'], dividend_yield_history=history,
                latest_price=close, latest_price_date=day.isoformat() if day else None,
                dividend_annual_per_share=annual.get(year), dividend_fiscal_year=year)

    def calculate(day, source=None):
        source = source or JoinQuantSource(day, settings['batch_size'])
        def progress(stage, processed=None, total=None):
            suffix = ' · {}/{} 只'.format(processed, total) if total is not None else ''
            _progress_note(stage + suffix)
        items, outcomes, stocks = screen_cash_cows(source, day.year - 1, progress)
        updated = china_now().isoformat() + '+08:00'
        run_id, payload = uuid.uuid4().hex, {}
        for period in PERIODS:
            if len(outcomes[period]) != len(stocks):
                raise ValueError('奶牛股分类未完成，不替换数据库结果。')
            items[period].sort(key=lambda row: (-row['average_yield'], row['code']))
            excluded = sum(v == 'excluded' for v in outcomes[period].values())
            insufficient = sum(v == 'insufficient' for v in outcomes[period].values())
            snapshot = dict(as_of=day.isoformat(), updated_at=updated, run_id=run_id,
                years=period, start_year=day.year - period, end_year=day.year - 1,
                universe_count=len(stocks), evaluated_count=excluded + len(items[period]),
                excluded_count=excluded, insufficient_count=insufficient, matched_count=len(items[period]),
                source='聚宽', rule_version=RULE_VERSION)
            payload[str(period)] = dict(snapshot=snapshot, items=items[period])
        # Do not let an unavailable data source turn the whole market into a valid empty selection.
        if all(row['snapshot']['evaluated_count'] == 0 for row in payload.values()):
            raise ValueError('全市场均缺少可判定资料，保留已有结果。')
        json.dumps(payload, allow_nan=False)
        return payload

    def save(Session, day, payload):
        with session_scope(Session) as session:
            old = session.query(CashCowSnapshot).filter_by(id=1).with_for_update().first()
            if old is not None and old.as_of_date > day:
                raise ValueError('历史日期不能覆盖更新的奶牛股快照。')
            for period, result in payload.items():
                previous = (old.payload.get(period, {}).get('snapshot', {}) if old else {})
                result['snapshot'].update(previous_updated_at=previous.get('updated_at'),
                    previous_previous_updated_at=previous.get('previous_updated_at'))
            if old is None:
                old = CashCowSnapshot(id=1)
                session.add(old)
            old.as_of_date, old.updated_at, old.payload = day, china_now(), payload
            session.commit()

    def run(end_date=settings['end_date'], write_database=True, force_refresh=False):
        if not all(isinstance(value, bool) for value in (write_database, force_refresh)):
            raise ValueError('write_database、force_refresh 必须为布尔值。')
        moment = china_now()
        day = as_date(end_date) if end_date else moment.date()
        if day > moment.date():
            raise ValueError('采集日期不能晚于今天。')
        days = _apis['get_trade_days'](end_date=_closed_day(day, moment), count=1)
        if days is None or len(days) != 1:
            raise ValueError('交易日历不可用。')
        day = as_date(days[0])
        if day > _closed_day(as_date(end_date) if end_date else moment.date(), moment):
            raise ValueError('交易日历返回未收市或未来日期。')
        _progress_note('检查奶牛股最近结果')
        engine = connect_database(_config) if write_database else None
        try:
            if engine is not None:
                Base.metadata.create_all(engine)
                Session = sessionmaker(bind=engine)
                with session_scope(Session) as session:
                    old = session.query(CashCowSnapshot).filter_by(id=1).first()
                    if (not force_refresh and old is not None and old.as_of_date == day
                            and set(old.payload) == {'3', '5', '10'}
                            and all(r['snapshot'].get('rule_version') == RULE_VERSION for r in old.payload.values())):
                        _progress_note('奶牛股当日已采集，跳过重复计算')
                        return preview(old.payload)
            payload = calculate(day)
            if engine is not None:
                save(Session, day, payload)
            return preview(payload)
        finally:
            if engine is not None:
                engine.dispose()

    def preview(payload):
        return pd.DataFrame([dict(years=int(p), code=r['code'], name=r['name'],
            average_yield=r['average_yield'], current_yield=r.get('current_yield'))
            for p, result in payload.items() for r in result['items']],
            columns=['years', 'code', 'name', 'average_yield', 'current_yield'])

    return SimpleNamespace(**locals())
