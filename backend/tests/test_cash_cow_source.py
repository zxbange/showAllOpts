"""Offline tests; numeric samples captured from live Eastmoney responses."""
import httpx
import pytest
from app.services.cash_cow_source import EastmoneyCashCowSource, CashCowSourceError


def source(payload):
    s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload))))
    s.capital = {code: [dict(END_DATE='2000-01-01', TOTAL_SHARES=100)] for code in ['600900','600519']}
    return s


def report(rows):
    return dict(success=True, result=dict(count=len(rows), pages=1, data=rows))


def test_live_histogram_units_and_missing():
    s = source(report([dict(SECUCODE='600900.SH', SECURITY_CODE='600900',
        REPORT_DATE='2015-12-31', DIVIDEND_IMPLE=7853110000, PARENTNETPROFIT=18234662090.59,
        DIVIDEND_PAY_IMPLE=43.066934615984)]))
    row = s.dividends(2015, codes=['600900'])['600900']
    assert row['cash_dividend'] == 7853110000
    assert row['payout_ratio'] == 43.066934615984
    assert s.dividends(2015, codes=['000001']) == {}
    s = source(report([dict(SECUCODE='600900.SH', SECURITY_CODE='600900',
                           REPORT_DATE='2015-12-31', KCFJCXSYJLR=None, PARENTNETPROFIT=1, NETCASH_OPERATE_PK='-')]))
    assert s.financials(2015)['600900'] == dict(adjusted_profit=None, net_profit=1, operating_cashflow=None)


def test_pagination_complete():
    calls = []
    def handler(request):
        page = int(request.url.params['pageNumber'])
        calls.append(page)
        rows = [dict(SECUCODE=f'{i:06}.SH', SECURITY_CODE=f'{i:06}', REPORT_DATE='2024-12-31')
                for i in (range(500) if page == 1 else [500])]
        return httpx.Response(200, json=dict(success=True, result=dict(count=501, pages=2, data=rows)))
    s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(s.dividends(2024)) == 501
    assert calls == [1, 2]
    with pytest.raises(CashCowSourceError):
        source(dict(success=True, result=dict(count=501, pages=2, data=[]))).dividends(2024)


def test_duplicates_and_error_fail_closed():
    row = dict(SECUCODE='600900.SH', SECURITY_CODE='600900')
    with pytest.raises(CashCowSourceError): source(report([row,row])).dividends(2024)
    with pytest.raises(CashCowSourceError): source(dict(success=False)).financials(2024)


def test_universe_beijing():
    s = source(dict(rc=0,data=dict(total=1,diff=[dict(f12='920992',f13=0,f14='中科美菱',f26=20221018)])))
    assert s.universe() == [dict(code='920992',exchange='BJ',name='中科美菱')]
    assert s.secids['920992'] == '0.920992'


def event(year, paid, text, obj='全体股东'):
    return dict(REPORT_TIME=f'{year}-12-31',NOTICE_DATE=paid,INFO_CODE=paid,
        ASSIGN_PROGRESS='实施方案',PAY_CASH_DATE=paid,IMPL_PLAN_PROFILE=text,ASSIGN_OBJECT=obj)


def test_dps_sum_and_ambiguity():
    rows = [event(2024,'2024-12-20','10派238.82元'),event(2024,'2025-06-26','10派276.73元'),
        event(2015,'2016-07-18','10派4元','金沙江资产重组前公司老股东、配套融资的七家战略投资者')]
    dps = source(report(rows)).per_share_dividends('600519',2015,2024)
    assert dps[2024] == pytest.approx(51.555)
    assert dps[2015] is None
    assert 2017 not in dps


def test_daily_prices_reject_partial_year():
    p = dict(rc=0,data=dict(code='600900',dktotal=2,klines=[
        '2024-11-29,27.51,27.32,28.00,26.78,23260908,63898262094.00,4.42,-0.94,-0.26,9.69',
        '2024-12-31,27.39,29.55,29.93,27.30,22321354,64417336857.00,9.63,8.16,2.23,9.30']))
    assert source(p).prices('600900',2024,2024) == {2024:dict(year_average_close=None,price_date='2024-12-31',trading_days=2)}
    p['data']['klines'].append(p['data']['klines'][-1])
    with pytest.raises(CashCowSourceError): source(p).prices('600900',2024,2024)


def test_cancellation():
    class Cancelled(Exception): pass
    def cancel(): raise Cancelled()
    with pytest.raises(Cancelled): EastmoneyCashCowSource(None,check_cancel=cancel).universe()


def test_average_daily_and_current_ttm():
    from datetime import date, timedelta
    days = [date(2024,1,1) + timedelta(days=i) for i in range(366)]
    bars = [f"{d},1,{10 if i % 2 else 20},20,1,100,100,1,1,1,1" for i,d in enumerate(days)]
    s = source(dict(rc=0,data=dict(code='600900',dktotal=len(bars),klines=bars)))
    s.as_of = '2024-12-31'
    assert s.prices('600900',2024,2024)[2024]['year_average_close'] == 15
    s.events['600900'] = [event(2024,'2024-06-20','10派5元(含税,扣税后4元)')]
    result = s.current_dividend_yield('600900')
    assert result['dividend_ttm_per_share'] == .5
    assert result['current_yield'] == 5


def test_future_fiscal_split_invalidates_prior_year_basis():
    r = event(2025,'2025-06-20','10转2派3元')
    s = source(report([event(2024,'2025-01-20','10派5元'),r]))
    assert s.per_share_dividends('600900',2024,2024)[2024] is None


def test_ttm_missing_payment_date_is_unavailable():
    s = source({})
    s.as_of = '2024-12-31'
    s.events['600900'] = [event(2024, '', '10派5元')]
    s.daily['600900'] = [('2024-12-31', 10)]
    assert s.current_dividend_yield('600900')['current_yield'] is None


def test_no_distribution_phrase_is_not_split():
    from app.services.cash_cow_source import corporate_action
    assert not corporate_action('不分配不转增')
    assert corporate_action('10转2派3元')


def test_independent_capital_change_blocks_raw_basis():
    s = source(report([event(2024,'2025-06-01','10派5元')]))
    s.capital['600900'] = [dict(END_DATE='2000-01-01',TOTAL_SHARES=100),
                           dict(END_DATE='2024-04-01',TOTAL_SHARES=120,CHANGE_REASON='转增股上市')]
    assert s.per_share_dividends('600900',2024,2024)[2024] is None


def test_daily_truncation_count_fails_closed():
    with pytest.raises(CashCowSourceError):
        source(dict(rc=0,data=dict(code='600900',dktotal=5156,klines=[]))).prices('600900',2016,2025)


def test_annual_date_must_match():
    for day in [None, '2023-12-31', 'invalid']:
        s = source(report([dict(SECUCODE='600900.SH',SECURITY_CODE='600900',REPORT_DATE=day)]))
        with pytest.raises(CashCowSourceError): s.dividends(2024)


def test_only_transient_failures_retry(monkeypatch):
    monkeypatch.setattr('app.services.cash_cow_source.time.sleep', lambda _: None)
    for status, attempts in [(503,2),(429,2),(403,1),(401,1)]:
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(status, json={})
        s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(CashCowSourceError): s.universe()
        assert len(calls) == attempts


def test_malformed_json_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b'not-json')
    s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(CashCowSourceError): s.universe()
    assert len(calls) == 1


def test_verified_provider_empty_response_only():
    assert source(dict(success=False,result=None,code=9201,message='返回数据为空')).dividends(2024) == {}
    for code, message in [(9701,'服务器繁忙'),(9201,'其他错误')]:
        with pytest.raises(CashCowSourceError):
            source(dict(success=False,result=None,code=code,message=message)).dividends(2024)


def test_complete_annual_cache_reused_across_instances(tmp_path):
    calls = []
    payload = report([dict(SECUCODE='600900.SH', SECURITY_CODE='600900',
                           REPORT_DATE='2024-12-31', DIVIDEND_IMPLE=100)])
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=payload)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(2):
        s = EastmoneyCashCowSource(client,cache_dir=tmp_path,as_of='2026-11-10')
        assert s.dividends(2024)['600900']['cash_dividend'] == 100
    assert len(calls) == 1
    assert not list(tmp_path.glob('.pending-*'))
    EastmoneyCashCowSource(client,cache_dir=tmp_path,as_of='2026-11-11').dividends(2024)
    assert len(calls) == 2


def test_partial_pages_never_cached(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=dict(success=True,result=dict(count=501,pages=2,data=[])))
    s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(handler)),cache_dir=tmp_path)
    for _ in range(2):
        with pytest.raises(CashCowSourceError): s.dividends(2024)
    assert len(calls) == 2
    assert not list(tmp_path.glob('*.json'))


def test_wrong_year_never_cached(tmp_path):
    payload = report([dict(SECUCODE='600900.SH',SECURITY_CODE='600900',REPORT_DATE='2023-12-31')])
    s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))),cache_dir=tmp_path)
    with pytest.raises(CashCowSourceError): s.dividends(2024)
    assert not list(tmp_path.glob('*.json'))


def test_rate_limit_cancellable_and_minimum(monkeypatch):
    clock = [1.0]
    sleeps = []
    monkeypatch.setattr('app.services.cash_cow_source.time.monotonic',lambda:clock[0])
    def sleep(seconds):
        sleeps.append(seconds);clock[0] += max(seconds, .000001)
    monkeypatch.setattr('app.services.cash_cow_source.time.sleep',sleep)
    s = source({})
    s._rate_limit();s._rate_limit()
    assert sum(sleeps) == pytest.approx(.2)


def test_current_quote_cache_expires_without_sliding_refresh(tmp_path, monkeypatch):
    s = EastmoneyCashCowSource(None,cache_dir=tmp_path,as_of='2026-11-10')
    clock = [1000.0]
    monkeypatch.setattr('app.services.cash_cow_source.time.time',lambda:clock[0])
    s._save_cache('daily',['600900'],{'data':{}})
    clock[0] += 299
    assert s._cached('daily',['600900']) == {'data':{}}
    clock[0] += 2
    assert s._cached('daily',['600900']) is None


def test_only_unambiguous_ordinary_issuance_preserves_share_unit():
    s = source({})
    for reason, expected in [('增发A股上市', True), ('增发A股上市,转增股上市', False),
                             ('未知变动', False), ('配股上市', False)]:
        s.capital['600900'] = [dict(END_DATE='2000-01-01', TOTAL_SHARES=100),
            dict(END_DATE='2024-06-01',TOTAL_SHARES=120,CHANGE_REASON=reason)]
        assert s._capital_stable('600900','2024-01-01') is expected


def test_universe_resume_does_not_request_network(tmp_path):
    payload = dict(rc=0,data=dict(total=1,diff=[dict(f12='920992',f13=0,f14='中科美菱')]))
    s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(
        lambda _:httpx.Response(200,json=payload))),cache_dir=tmp_path)
    expected = s.universe()
    resumed = EastmoneyCashCowSource(None,cache_dir=tmp_path)
    assert resumed.universe() == expected
    assert resumed.secids['920992'] == '0.920992'


def test_medium_universe_batches_use_disjoint_200_code_chunks():
    import re
    chunks = []
    def handler(request):
        codes = re.findall(r'"(\d{6})"',request.url.params['filter'])
        chunks.append(codes)
        return httpx.Response(200,json=report([dict(SECUCODE=code+'.SH',SECURITY_CODE=code,
            REPORT_DATE='2024-12-31',DIVIDEND_IMPLE=10) for code in codes]))
    s = EastmoneyCashCowSource(httpx.Client(transport=httpx.MockTransport(handler)))
    codes = [str(600000+i) for i in range(466)]
    assert len(s.dividends(2024,codes)) == 466
    assert list(map(len,chunks)) == [200,200,66]
    assert [code for chunk in chunks for code in chunk] == codes


def test_ancient_undated_noncash_does_not_poison_ttm():
    s = source({})
    s.as_of = '2026-11-10'
    s.events['600548'] = [event(2006,'','10送3.2','流通股股东'),
        event(2025,'2026-07-14','10派2.44元','A股股东'),
        event(2007,'','不分配不转增'),event(2008,'','10派0元')]
    s.capital['600548'] = [dict(END_DATE='2000-01-01',TOTAL_SHARES=100)]
    s.daily['600548'] = [('2026-11-10',8.69)]
    result = s.current_dividend_yield('600548')
    assert result['dividend_ttm_per_share'] == pytest.approx(.244)
    assert result['current_yield'] == pytest.approx(100*.244/8.69)
    s.capital['600548'] = []
    assert s.current_dividend_yield('600548')['current_yield'] is None


def test_undated_unknown_cash_still_blocks_ttm():
    for text in ['', '未知方案', '10派2.44元']:
        s = source({})
        s.as_of = '2026-11-10'
        s.events['600900'] = [event(2006,'',text)]
        s.daily['600900'] = [('2026-11-10',10)]
        assert s.current_dividend_yield('600900')['current_yield'] is None
