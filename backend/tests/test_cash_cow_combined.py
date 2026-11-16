from types import SimpleNamespace
from unittest.mock import patch
from pydantic import SecretStr
import httpx
import pytest
from app.services.cash_cow_combined import CombinedCashCowSource
from app.services.cash_cow_source import CashCowSourceError, EastmoneyCashCowSource


def source(handler):
    with patch('app.services.cash_cow_combined.integrations_config', return_value={'asharehub': {'token': 'private-ash'}, 'tushare': {'token': 'private-ts'}}):
        obj = CombinedCashCowSource(httpx.Client(transport=httpx.MockTransport(handler)),
        settings=SimpleNamespace(asharehub_api_key=SecretStr('private-ash'), tushare_token=SecretStr('private-ts')),
        as_of='2026-11-16')
    obj.symbols={'600900':'600900.SH'}
    obj._calendar=lambda *a:{'20240102','20241231'}
    return obj


def payload(items):
    return dict(code=0,data=dict(fields=['ts_code','trade_date','close'],items=items))


def test_raw_prices_shared_annual_mean(monkeypatch):
    monkeypatch.setattr('app.services.cash_cow_combined.time.sleep',lambda _:None)
    obj=source(lambda req:httpx.Response(200,json=payload([
        ['600900.SH','20261201',10]])))
    with pytest.raises(CashCowSourceError):obj.prices('600900',2024,2024)
    obj=source(lambda req:httpx.Response(200,json=payload([
        ['600900.SH','20241231',20],['600900.SH','20240102',10],['600900.SH','20261113',25]])))
    assert obj.prices('600900',2024,2024)[2024]['year_average_close']==15
    assert obj.daily['600900'][-1]==('2026-11-13',25)


@pytest.mark.parametrize('data',[payload([]),payload([['600900.SH','20240102',1]]*6000),
    payload([['600900.SH','20240102',1],['600900.SH','20240102',1]]),
    payload([['600901.SH','20240102',1]]),dict(code=40203,data=None)])
def test_bad_prices_fail_closed(data):
    obj=source(lambda req:httpx.Response(200,json=data))
    with pytest.raises(CashCowSourceError):obj.prices('600900',2024,2024)


def test_secret_errors_not_chained():
    obj=source(lambda req:httpx.Response(403,json={'detail':'private-ash'}))
    with pytest.raises(CashCowSourceError) as caught:obj.prices('600900',2024,2024)
    assert 'private' not in str(caught.value)
    assert caught.value.__cause__ is None


def test_financials_exact_mapping_and_missing(monkeypatch):
    monkeypatch.setattr(EastmoneyCashCowSource,'financials',lambda *a,**kw:{'600900':dict(net_profit=100,operating_cashflow=120,adjusted_profit=90)})
    obj=source(lambda req:None)
    obj._ash=lambda ep,p:[dict(symbol='600900.SH',end_date='20241231',ann_date='20250401',report_type='1',n_income_attr_p=100,n_cashflow_act=120)]
    assert obj.financials(2024,['600900'])['600900']['adjusted_profit']==90
    obj._ash=lambda ep,p:[]
    assert obj.financials(2024,['600900'])['600900']['net_profit']==100


def test_three_periods_share_new_boundary():
    from app.services.cash_cow_rules import screen_cash_cows
    class Fake:
        price_calls=0
        def universe(self):return [dict(code='600900',name='Test',exchange='SH')]
        def dividends(self,y,codes=None):return {'600900':{'cash_dividend':100}}
        def financials(self,y,codes=None):return {'600900':dict(adjusted_profit=120,net_profit=100,operating_cashflow=100)}
        def per_share_dividends_by_period(self,c,y,periods):return {p:{year:5 for year in range(y-p+1,y+1)}for p in periods}
        def prices(self,c,start,end):
            self.price_calls+=1
            return {y:dict(year_average_close=50,price_date=f'{y}-12-31')for y in range(start,end+1)}
        def current_dividend_yield(self,c):return {}
    obj=Fake();items,outcomes,_=screen_cash_cows(obj,2025,lambda *a:None)
    assert obj.price_calls==1
    assert all(len(items[p])==1 and outcomes[p]['600900']=='matched' for p in (3,5,10))


def test_conflicting_same_day_crosscheck_does_not_replace_authoritative(monkeypatch):
    monkeypatch.setattr(EastmoneyCashCowSource,'financials',lambda *a,**kw:{'600900':dict(net_profit=100,operating_cashflow=120,adjusted_profit=90)})
    obj=source(lambda req:None)
    row=dict(symbol='600900.SH',end_date='20241231',ann_date='20250401',report_type='1',n_income_attr_p=100,n_cashflow_act=120)
    obj._ash=lambda ep,p:[row,dict(row,n_income_attr_p=101)]
    assert obj.financials(2024,['600900'])['600900']['net_profit'] == 100


def test_live_configuration_snapshot_not_startup_settings():
    old = SimpleNamespace(asharehub_api_key=SecretStr('old'),tushare_token=SecretStr('old'))
    with patch('app.services.cash_cow_combined.integrations_config',return_value={'asharehub':{'token':'new-ash'},'tushare':{'token':'new-ts'}}) as read:
        obj=CombinedCashCowSource(None,settings=old)
        assert (obj.ash_key,obj.ts_token)==('new-ash','new-ts')
        read.assert_called_once_with(old)


def test_stock_pool_partial_response_rejected():
    obj=source(lambda req:None)
    symbols=['600900.SH','000001.SZ','920000.BJ']
    obj._ash=lambda *a:[dict(symbol=s,name='Test',list_date='20200101',list_status='L')for s in symbols]
    obj._selector_universe=lambda:[dict(code=s.split('.')[0],exchange=s.split('.')[1],list_date='2020-01-01')for s in symbols+['600036.SH']]
    with pytest.raises(CashCowSourceError,match='覆盖不一致'):obj.universe()


def test_invalid_ash_cache_not_reused():
    obj=source(lambda req:None)
    obj._cached=lambda *a: [{'symbol':'bad'}]
    obj._save_cache=lambda *a:pytest.fail('unvalidated response cached')
    obj._request=lambda *a:[{'symbol':'fresh'}]
    assert obj._ash('financials/income',{})==[{'symbol':'fresh'}]


def test_middle_trading_day_missing_audited_not_assumed_suspended():
    obj=source(lambda req:httpx.Response(200,json=payload([
        ['600900.SH','20240102',10],['600900.SH','20241231',20]])))
    obj._calendar=lambda *a:{'20240102','20240603','20241231'}
    row=obj.prices('600900',2024,2024)[2024]
    assert row['year_average_close']==15 and row['missing_open_days']==1
    assert row['suspension_verified'] is False


def test_truncated_calendar_rejected():
    obj=source(lambda req:None)
    obj._request=lambda *a:[dict(exchange='SSE',cal_date='20240102',is_open=1)]
    with pytest.raises(CashCowSourceError,match='日历不完整'):
        CombinedCashCowSource._calendar(obj,2024,2024,'SH')


def test_calendar_shared_between_periods_and_markets():
    from datetime import date,timedelta
    obj=source(lambda req:None)
    days=(date(2025,12,31)-date(2016,1,1)).days+1
    rows=[[(date(2016,1,1)+timedelta(days=n)).strftime('%Y%m%d'),1]for n in range(days)]
    calls=[]
    obj._request=lambda *args:(calls.append(args)or [dict(exchange=args[-1]['exchange'],cal_date=d,is_open=o) for d,o in rows])
    first=CombinedCashCowSource._calendar(obj,2023,2025,'SH')
    second=CombinedCashCowSource._calendar(obj,2016,2025,'SZ')
    third=CombinedCashCowSource._calendar(obj,2021,2025,'SH')
    assert len(calls)==2 and len(first)<len(third)<len(second)
    assert calls[0][0]=='asharehub' and calls[0][1]=='reference/trade-calendar'

@pytest.mark.parametrize('listed,as_of,eligible', [
    ('2023-11-16','2026-11-15',False),('2023-11-16','2026-11-16',True),
    ('2023-11-16','2026-11-17',True),('2020-02-29','2023-02-27',False),
    ('2020-02-29','2023-02-28',True),('2021-03-01','2024-02-29',False)])
def test_exact_third_listing_anniversary(listed, as_of, eligible):
    from app.services.cash_cow_rules import listed_three_years
    assert listed_three_years(listed, as_of) is eligible


def test_age_filter_precedes_all_financial_and_price_requests():
    from app.services.cash_cow_rules import screen_cash_cows
    class Young:
        as_of='2026-11-16'
        def universe(self):return [dict(code='600900',name='Test',exchange='SH',list_date='2023-11-17')]
        def dividends(self,*a,**k):pytest.fail('young stock requested')
        def financials(self,*a,**k):pytest.fail('young stock requested')
        def prices(self,*a,**k):pytest.fail('young stock requested')
    items,outcomes,stocks=screen_cash_cows(Young(),2025,lambda *a:None)
    assert len(stocks)==1
    assert all(items[p]==[] and outcomes[p]['600900']=='excluded' for p in (3,5,10))


def test_age_eligible_still_requires_complete_fiscal_years():
    from app.services.cash_cow_rules import screen_cash_cows
    class Mature:
        as_of='2026-11-16'
        years=[]
        def universe(self):return [dict(code='600900',name='Test',exchange='SH',list_date='2022-11-16')]
        def dividends(self,y,codes=None):
            self.years.append(y)
            return {'600900':dict(cash_dividend=100)}
        def financials(self,y,codes=None):return {'600900':dict(adjusted_profit=120,net_profit=100,operating_cashflow=100)}
        def per_share_dividends_by_period(self,c,y,periods):
            assert periods==[3]
            return {3:{year:5 for year in (2023,2024,2025)}}
        def prices(self,c,start,end):return {y:dict(year_average_close=50,price_date=f'{y}-12-31') for y in range(start,end+1)}
        def current_dividend_yield(self,c):return {}
    obj=Mature();items,outcomes,_=screen_cash_cows(obj,2025,lambda *a:None)
    assert obj.years==[2025,2024,2023]
    assert len(items[3])==1
    assert all(outcomes[p]['600900']=='insufficient' for p in (5,10))


def test_listing_date_disagreement_fails_before_filter():
    obj=source(lambda req:None)
    symbols=['600900.SH','000001.SZ','920000.BJ']
    obj._ash=lambda *a:[dict(symbol=s,name='Test',list_date='20200101',list_status='L') for s in symbols]
    obj._selector_universe=lambda:[dict(code=s.split('.')[0],exchange=s.split('.')[1],list_date='2021-11-15' if s.endswith('.BJ') else '2020-01-01') for s in symbols]
    with pytest.raises(CashCowSourceError,match='上市日期跨来源不一致'):obj.universe()


def test_bj_calendar_not_inferred_from_sse():
    obj=source(lambda req:None)
    obj._request=lambda *a:pytest.fail('must not infer a calendar')
    with pytest.raises(CashCowSourceError,match='北交所交易日历'):
        CombinedCashCowSource._calendar(obj,2024,2025,'BJ')


def test_calendar_unavailable_does_not_block_trading_day_mean():
    obj=source(lambda req:httpx.Response(200,json=payload([['600900.SH','20241010',20]])))
    def unavailable(*a):raise CashCowSourceError('unavailable')
    obj._calendar=unavailable
    row=obj.prices('600900',2024,2024)[2024]
    assert row['year_average_close']==20 and row['missing_open_days'] is None
    assert row['price_basis']=='returned_unadjusted_trading_days'


def test_whole_requested_year_missing_rejected():
    obj=source(lambda req:httpx.Response(200,json=payload([['600900.SH','20241231',20]])))
    with pytest.raises(CashCowSourceError,match='2023 年无有效'):
        obj.prices('600900',2023,2024)


def test_returned_day_mean_accepts_non_december_last_trade():
    from app.services.cash_cow_rules import finish_stock
    years=[dict(year=y,payout_ratio=50,cashflow_coverage=2) for y in (2023,2024,2025)]
    prices={y:dict(year_average_close=10,price_date=f'{y}-10-01',price_basis='returned_unadjusted_trading_days',trading_days=190,missing_open_days=53,suspension_verified=False) for y in (2023,2024,2025)}
    outcome,item=finish_stock(dict(code='600900',name='Test',exchange='SH'),years,{y:1 for y in (2023,2024,2025)},prices,3)
    assert outcome=='matched' and item['years'][0]['missing_open_days']==53


@pytest.mark.parametrize('endpoint,field,target,expected,actual', [
    ('financials/income', 'n_income_attr_p', 'net_profit', 100, 102),
    ('financials/cash-flow', 'n_cashflow_act', 'operating_cashflow', 120, 123),
])
def test_financial_mismatch_logs_public_provenance_only(monkeypatch, caplog, endpoint, field, target, expected, actual):
    monkeypatch.setattr(EastmoneyCashCowSource, 'financials', lambda *a, **kw: {
        '600900': dict(net_profit=100, operating_cashflow=120, adjusted_profit=90)})
    obj = source(lambda req: None)
    def rows(ep, params):
        row = dict(symbol='600900.SH', end_date='20241231', ann_date='20250401',
                   f_ann_date='20250402', report_type='1', n_income_attr_p=100,
                   n_cashflow_act=120, token='never-log-this', raw_url='secret-url')
        if ep == endpoint:
            row[field] = actual
        return [row]
    obj._ash = rows
    obj.financial_metadata[2024] = {'600900': {'NOTICE_DATE': '2025-04-01 00:00:00', 'UPDATE_DATE': '2026-04-01 00:00:00'}}
    assert obj.financials(2024, ['600900'])['600900']['net_profit'] == 100
    message = caplog.text
    for fragment in ('Cash cow financial mismatch', 'symbol=600900.SH', 'year=2024',
                     'endpoint=' + endpoint, 'field=' + target,
                     'asharehub_value=' + str(actual), 'eastmoney_value=' + str(expected),
                     'difference=' + str(actual - expected), 'tolerance=0.01',
                     'asharehub_ann_date=20250401', 'asharehub_f_ann_date=20250402',
                     'eastmoney_report_version=unavailable', 'action=use_authoritative_annual',
                     'eastmoney_notice_date=2025-04-01', 'eastmoney_update_date=2026-04-01'):
        assert fragment in message
    assert 'never-log-this' not in message and 'secret-url' not in message
    assert 'private-ash' not in message and 'private-ts' not in message


def test_financial_mismatch_uses_selected_version_and_sanitizes_unused_date(monkeypatch, caplog):
    monkeypatch.setattr(EastmoneyCashCowSource, 'financials', lambda *a, **kw: {
        '600900': dict(net_profit=100, operating_cashflow=120, adjusted_profit=90)})
    obj = source(lambda req: None)
    row = dict(symbol='600900.SH', end_date='20241231', report_type='1',
               n_income_attr_p=102, n_cashflow_act=120, f_ann_date='20250402',
               ann_date='private-ash\nBAD')
    obj._ash = lambda *a: [row, dict(row, f_ann_date='20250401', n_income_attr_p=100)]
    assert obj.financials(2024, ['600900'])['600900']['net_profit'] == 100
    assert 'asharehub_value=102' in caplog.text
    assert 'asharehub_f_ann_date=20250402' in caplog.text
    assert 'asharehub_ann_date=-' in caplog.text and 'private-ash' not in caplog.text


def test_financial_crosscheck_tolerance_not_relaxed(monkeypatch, caplog):
    expected = 100_000_000
    monkeypatch.setattr(EastmoneyCashCowSource, 'financials', lambda *a, **kw: {
        '600900': dict(net_profit=expected, operating_cashflow=120, adjusted_profit=90)})
    obj = source(lambda req: None)
    row = dict(symbol='600900.SH', end_date='20241231', ann_date='20250401', report_type='1',
               n_income_attr_p=expected + 1, n_cashflow_act=120)
    obj._ash = lambda *a: [row]
    assert obj.financials(2024, ['600900'])['600900']['net_profit'] == expected
    row['n_income_attr_p'] += .01
    assert obj.financials(2024, ['600900'])['600900']['net_profit'] == expected
    assert 'tolerance=1 ' in caplog.text


@pytest.mark.parametrize('missing,actual', [(False, 1557000775.65), (True, None)])
def test_restated_em_basis_is_not_mixed_with_original_crosscheck(monkeypatch, missing, actual):
    authoritative = dict(net_profit=1557132327.91, operating_cashflow=2850716944.11,
                         adjusted_profit=1441714437.09)
    monkeypatch.setattr(EastmoneyCashCowSource, 'financials', lambda *a, **kw: {'600900': dict(authoritative)})
    obj = source(lambda req: None)
    obj._ash = lambda *a: ([] if missing else [dict(symbol='600900.SH', end_date='20241231',
        ann_date='20250318', report_type='1', n_income_attr_p=actual, n_cashflow_act=2170810025.93)])
    assert obj.financials(2024, ['600900']) == {'600900': authoritative}


def test_crosscheck_unavailable_does_not_invent_missing_em_fields(monkeypatch, caplog):
    authoritative = dict(net_profit=None, operating_cashflow=120, adjusted_profit=90)
    monkeypatch.setattr(EastmoneyCashCowSource, 'financials', lambda *a, **kw: {'600900': dict(authoritative)})
    obj = source(lambda req: None)
    def unavailable(*a):
        raise CashCowSourceError('secret-url private-ash')
    obj._ash = unavailable
    assert obj.financials(2024, ['600900']) == {'600900': authoritative}
    assert 'comparison_basis=unverified' in caplog.text
    assert 'secret-url' not in caplog.text and 'private-ash' not in caplog.text


def test_authoritative_financial_failure_still_fails_closed(monkeypatch):
    def invalid(*a, **kw):
        raise CashCowSourceError('年度报表日期缺失或与请求年份不符')
    monkeypatch.setattr(EastmoneyCashCowSource, 'financials', invalid)
    obj = source(lambda req: None)
    obj._ash = lambda *a: pytest.fail('must not replace authoritative failure')
    with pytest.raises(CashCowSourceError, match='年度报表'):
        obj.financials(2024, ['600900'])


def test_crosscheck_cancellation_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(EastmoneyCashCowSource, 'financials', lambda *a, **kw: {})
    obj = source(lambda req: None)
    def unavailable(*a):raise CashCowSourceError('unavailable')
    def cancelled():raise RuntimeError('cancelled')
    obj._ash = unavailable
    obj.check_cancel = cancelled
    with pytest.raises(RuntimeError, match='cancelled'):
        obj.financials(2024)


def pool_source(extra_date, side='eastmoney_selector'):
    obj=source(lambda req:None)
    symbols=['600900.SH','000001.SZ','920000.BJ']
    ash=[dict(symbol=s,name='Test',list_date='20200101',list_status='L') for s in symbols]
    em=[dict(code=s[:6],exchange=s[7:],name='Test',list_date='2020-01-01') for s in symbols]
    if side=='eastmoney_selector':
        em.append(dict(code='920309',exchange='BJ',name='Test',list_date=extra_date))
    else:
        ash.append(dict(symbol='920309.BJ',name='Test',list_date=extra_date.replace('-',''),list_status='L'))
    obj._ash=lambda *a:ash
    obj._selector_universe=lambda:em
    return obj


@pytest.mark.parametrize('side',['asharehub','eastmoney_selector'])
@pytest.mark.parametrize('listed,allowed',[('2023-11-17',True),('2023-11-16',False),('2020-11-16',False)])
def test_only_provably_young_pool_differences_allowed(side,listed,allowed):
    obj=pool_source(listed,side)
    if allowed:
        stocks=obj.universe()
        assert len(stocks)==(4 if side=='eastmoney_selector' else 3)
    else:
        with pytest.raises(CashCowSourceError,match='覆盖不一致'):obj.universe()


@pytest.mark.parametrize('listed',[None,'','invalid','2026-02-30','2026-11-17'])
def test_unverified_pool_difference_dates_fail(listed):
    with pytest.raises(CashCowSourceError):pool_source(listed).universe()


def test_em_only_new_listing_cannot_request_financials():
    from app.services.cash_cow_rules import screen_cash_cows
    obj=pool_source('2026-11-16')
    def dividends(year,codes=None):
        assert '920309' not in codes
        return {code:{'cash_dividend':0} for code in codes}
    obj.dividends=dividends
    obj.financials=lambda *a,**k:{}
    items,outcomes,stocks=screen_cash_cows(obj,2025,lambda *a:None)
    assert all(outcomes[p]['920309']=='excluded' for p in (3,5,10))
    assert len(stocks)==4


def test_delisted_ash_record_not_silently_accepted():
    obj=pool_source('2026-11-16','asharehub')
    rows=obj._ash();rows[-1]['list_status']='D'
    obj._ash=lambda *a:rows
    with pytest.raises(CashCowSourceError):obj.universe()


def test_shared_young_listing_date_conflict_not_hidden_by_age_filter():
    obj=pool_source('2026-11-16')
    rows=obj._ash()
    rows.append(dict(symbol='920309.BJ',name='Test',list_date='20200101',list_status='L'))
    obj._ash=lambda *a:rows
    with pytest.raises(CashCowSourceError,match='上市日期跨来源不一致'):obj.universe()
