from datetime import date
from decimal import Decimal

import pytest

from app.services.dividend_public_source import (
    DividendPublicSource, DividendSourceError, mature, rank_records, suspended,
)

DAY = date(2026, 11, 16)
SYMBOL = '605255.SH'


def quote(volume=0, **changes):
    return dict(dict(trade_time='2026-11-16T15:00:00', volume=volume), **changes)


def event(start='2026-11-16 09:30:00', end=None):
    return dict(SECUCODE=SYMBOL, SUSPEND_START_TIME=start, SUSPEND_END_TIME=end)


def test_current_suspension_and_resumption():
    assert suspended(SYMBOL, quote(), [event()], DAY)
    assert not suspended(SYMBOL, quote(100), [event(end='2026-11-13 15:00:00', start='2026-11-02 09:30:00')], DAY)
    assert not suspended(SYMBOL, quote(100), [event(start='2026-11-16 10:00:00', end='2026-11-16 10:10:00')], DAY)


@pytest.mark.parametrize('q,events', [
    (quote(), []),
    (quote(100), [event()]),
    (quote(trade_time='2026-11-13T15:00:00'), []),
    (quote(trade_time='2026-11-16T10:00:00'), []),
    (quote(-1), [event()]),
    (quote(None), []),
])
def test_unknown_or_conflicting_state_does_not_become_exclusion(q, events):
    with pytest.raises(DividendSourceError):
        suspended(SYMBOL, q, events, DAY)


def test_age_boundary():
    assert mature('2023-11-16', DAY)
    assert not mature('2023-11-17', DAY)
    assert mature('2020-02-29', date(2023, 2, 28))


def fixtures():
    stocks = [dict(symbol='600036.SH', name='Bank'), dict(symbol='600900.SH', name='Other')]
    vals = {r['symbol']: dict(pe_ttm=7, total_mv=1000000, close=10, pb=1) for r in stocks}
    cash = {r['symbol']: dict(fiscal_year=2025, annual_dps={2025: Decimal('.5'), 2024: Decimal('.4'), 2023: Decimal('.3')}) for r in stocks}
    industries = {'600036.SH': dict(l1_code='801780.SI', l1_name='Bank'),
                  '600900.SH': dict(l1_code='801160.SI', l1_name='Utility')}
    return stocks, vals, cash, industries


def test_rank_cash_and_cap_units_and_independent_groups():
    meta, rows = rank_records(*fixtures(), DAY, 5500)
    assert len(rows) == 2
    assert {r['method'] for r in rows} == {'value', 'bank'}
    assert all(r['rank'] == 1 for r in rows)
    assert all(r['dividend_yield_1y'] == Decimal('5') for r in rows)
    assert all(r['dividend_cash_1y'] is None and r['dividend_per_share'] == Decimal('.5') and r['market_cap'] == 100 for r in rows)
    assert all([p['yield_pct'] for p in r['dividend_yield_history']] == [5, None, None] for r in rows)
    assert meta['value_count'] == meta['value_candidates'] == 2


@pytest.mark.parametrize('pe,rate', [(12, 5), (0, 5), (-2, 5), (None, 5), (7, 3)])
def test_strict_original_thresholds(pe, rate):
    stocks, vals, cash, industries = fixtures()
    for value in vals.values(): value['pe_ttm'] = pe
    for value in cash.values(): value['annual_dps'][2025] = Decimal(str(rate)) / 10
    assert rank_records(stocks, vals, cash, industries, DAY, 5500)[1] == []


def test_missing_annual_cash_not_zero():
    stocks, vals, cash, industries = fixtures()
    del cash['600036.SH']['annual_dps'][2024]
    _, rows = rank_records(stocks, vals, cash, industries, DAY, 5500)
    assert rows[0]['dividend_yield_history'][1]['yield_pct'] is None


def test_missing_proposal_source_cannot_publish():
    source = object.__new__(DividendPublicSource)
    source.day = DAY
    source.check_cancel = lambda: None
    source.proposal_rows = lambda _: []
    with pytest.raises(DividendSourceError, match='方案'):
        source.fiscal_cash(['600036.SH'])


@pytest.mark.parametrize('young_side', [None, 'asharehub', 'eastmoney_selector'])
def test_collect_complete_current_day_and_missing_nonpaused_valuation(monkeypatch, young_side):
    from datetime import datetime
    import app.services.dividend_public_source as module
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):return cls(2026,11,16,23,0,tzinfo=tz)
    monkeypatch.setattr(module,'datetime',Clock)
    source=object.__new__(DividendPublicSource)
    source.enrich_history=lambda records: None
    source.day=DAY
    source.check_cancel=lambda:None
    stocks=[dict(symbol='600036.SH',name='Bank',list_date='20000101',list_status='L'),
            dict(symbol='605255.SH',name='Paused',list_date='20000101',list_status='L')]
    quotes=[dict(symbol=r['symbol'],name=r['name'],trade_time='2026-11-16T15:00:00',
                 volume=0 if r['symbol']==SYMBOL else 100,price=10) for r in stocks]
    valuations=[dict(symbol='600036.SH',trade_date='20261116',pe_ttm=7,total_mv=1000000,close=10,pb=1)]
    responses={'reference/stocks':stocks,'market/realtime':quotes,'market/fundamentals':valuations,
               'reference/industries':[dict(symbol='600036.SH',l1_code='801780.SI',l1_name='Bank'),
                                       dict(symbol='T00018.SH',l1_code='801780.SI',l1_name='Legacy')]}
    source.ash=lambda endpoint,params:responses[endpoint]
    independent=[dict(code=r['symbol'][:6],exchange='SH',name=r['name'],list_date='2000-01-01') for r in stocks]
    source._selector_universe=lambda:independent
    if young_side == 'asharehub':
        stocks.append(dict(symbol='920309.BJ', name='Young', list_date='20261116', list_status='L'))
    elif young_side == 'eastmoney_selector':
        independent.append(dict(code='920309', exchange='BJ', name='Young', list_date='2026-11-16'))
    source.suspension_events=lambda:[event()]
    def dividends(symbols):
        assert symbols==['600036.SH']
        return {'600036.SH':dict(fiscal_year=2025,annual_dps={2025:Decimal('.5'),2024:Decimal('.4'),2023:Decimal('.3')})}
    source.fiscal_cash=dividends
    independent[0]['name'] = 'Bank新简称'
    meta,records=source.collect()
    assert meta['eligible_count']==1 and meta['value_count']==1
    assert records[0]['method']=='bank' and records[0]['dividend_cash_1y'] is None
    independent[0]['name'] = 'ＳＴ Bank'
    with pytest.raises(DividendSourceError, match='ST/退市标识'):
        source.collect()
    independent[0]['name'] = 'Bank新简称'
    quotes[0]['name'] = '＊ＳＴ Bank'
    with pytest.raises(DividendSourceError, match='ST/退市标识'):
        source.collect()
    quotes[0]['name'] = 'Bank'
    independent[0]['name'] = ' '
    with pytest.raises(DividendSourceError, match='证券名称缺失'):
        source.collect()
    independent[0]['name'] = 'Bank'
    industry_rows=responses['reference/industries']
    responses['reference/industries']=[industry_rows[-1]]
    with pytest.raises(DividendSourceError,match='申万'):
        source.collect()
    responses['reference/industries']=industry_rows
    responses['market/fundamentals']=[]
    with pytest.raises(DividendSourceError,match='非停牌'):
        source.collect()


def test_verified_full_day_halt_requires_no_current_quote_or_valuation():
    assert suspended(SYMBOL, quote(999, trade_time='2026-11-13T15:00:00'), [event()], DAY)
    assert suspended(SYMBOL, None, [event()], DAY)
    assert suspended(SYMBOL, quote(None), [event()], DAY)
    with pytest.raises(DividendSourceError):
        suspended(SYMBOL, None, [], DAY)


def test_public_source_refuses_historical_current_snapshot_replay(monkeypatch):
    from datetime import datetime
    import app.services.dividend_public_source as module
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,11,16,23,tzinfo=tz)
    monkeypatch.setattr(module,'datetime',Clock)
    source=object.__new__(DividendPublicSource)
    source.enrich_history=lambda records: None
    source.day=date(2026,11,13)
    source.ash=lambda *args:pytest.fail('Historical requests must stop before network')
    with pytest.raises(DividendSourceError,match='历史'):
        source.collect()
