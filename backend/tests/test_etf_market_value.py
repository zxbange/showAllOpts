from datetime import date, datetime
from decimal import Decimal

from app.core.etf_catalog import ETF_GROUPS, GROUP_NAMES
from app.models.etf import ETFPrice
from test_etf_api import row


def price(day, value, code='510300.XSHG'):
    return ETFPrice(fund_code=code, trade_date=day, close_price=Decimal(value),
                    source='sse.dayk', updated_at=datetime(2026, 1, 8, 18))


def test_catalog_keeps_original_ten_and_has_seven_groups(client):
    assert len(ETF_GROUPS) == 7
    assert sum(len(funds) for funds in ETF_GROUPS.values()) == 18
    assert '515330.XSHG' in dict(ETF_GROUPS['hs300'])
    assert {'510510.XSHG', '510580.XSHG', '159922.XSHE'} <= dict(ETF_GROUPS['zz500']).keys()
    for group, funds in ETF_GROUPS.items():
        response = client.get('/api/v1/etf-shares/history', params={'group': group})
        assert response.status_code == 200
        assert len(response.json()['funds']) == len(funds)
        assert response.json()['group_options'] == [{'id': key, 'name': name} for key, name in GROUP_NAMES.items()]


def test_group_total_uses_same_day_prices_and_never_partial_sums(client, session):
    d1, d2, d3 = date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)
    codes = [code for code, _ in ETF_GROUPS['hs300']]
    for i, code in enumerate(codes):
        session.add_all([row(d1, 100, code), row(d2, 110, code), row(d3, 120, code)])
        session.add_all([price(d1, str(i + 10), code), price(d2, str(i + 10), code)])
    session.add_all([price(d3, str(i + 10), code) for i, code in enumerate(codes[:-1])])
    session.add_all([row(d1, 99999, '510500.XSHG'), price(d1, '100', '510500.XSHG')])
    session.commit()
    values = client.get('/api/v1/etf-shares/history').json()['market_values']
    assert [Decimal(p['market_value']) if p['market_value'] is not None else None for p in values] == [6000, 6600, None]
    assert [p['available_funds'] for p in values] == [5, 5, 4]
    assert all(p['expected_funds'] == 5 for p in values)
    session.add(price(d3, '14', codes[-1]))
    session.commit()
    assert Decimal(client.get('/api/v1/etf-shares/history').json()['market_values'][-1]['market_value']) == 7200


def test_missing_share_or_nonpositive_price_is_not_complete(client, session):
    day = date(2026, 1, 8)
    for code, _ in ETF_GROUPS['kc50']:
        session.add(price(day, '2', code))
    session.add(row(day, 0, '588000.XSHG'))
    session.commit()
    point = client.get('/api/v1/etf-shares/history?group=kc50').json()['market_values'][0]
    assert point['market_value'] is None and point['available_funds'] == 1
    session.add(row(day, 10, '588080.XSHG'))
    session.commit()
    assert Decimal(client.get('/api/v1/etf-shares/history?group=kc50').json()['market_values'][0]['market_value']) == 20
    session.get(ETFPrice, ('588080.XSHG', day)).close_price = Decimal(0)
    session.commit()
    assert client.get('/api/v1/etf-shares/history?group=kc50').json()['market_values'][0]['market_value'] is None


def test_decimal_precision_and_legacy_database_without_price_table(client, session, engine):
    day = date(2026, 1, 8)
    session.add_all([row(day, 9007199254740993, '159915.XSHE'), price(day, '1.12345678', '159915.XSHE')])
    session.commit()
    data = client.get('/api/v1/etf-shares/history?group=cyb').json()
    assert Decimal(data['market_values'][0]['market_value']) == Decimal(9007199254740993) * Decimal('1.12345678')
    ETFPrice.__table__.drop(engine)
    data = client.get('/api/v1/etf-shares/history?group=cyb').json()
    assert data['funds'][0]['shares'] == '9007199254740993'
    assert data['market_values'][0]['market_value'] is None


def test_whole_group_missing_share_day_stays_null_and_breaks_curve(client, session):
    d1, d2, d3 = date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)
    code = '159915.XSHE'
    session.add_all([row(d1, 10, code), row(d3, 30, code),
                     price(d1, '2', code), price(d2, '3', code), price(d3, '4', code)])
    session.commit()
    values = client.get('/api/v1/etf-shares/history?group=cyb').json()['market_values']
    assert [p['date'] for p in values] == [str(d1), str(d2), str(d3)]
    assert values[1]['market_value'] is None and values[1]['available_funds'] == 0
    # Even both sources missing on a verified exchange trading day must not connect across it.
    session.delete(session.get(ETFPrice, (code, d2)))
    session.commit()
    values = client.get('/api/v1/etf-shares/history?group=cyb').json()['market_values']
    assert [p['date'] for p in values] == [str(d1), str(d2), str(d3)]
    assert values[1]['market_value'] is None


def test_delayed_split_price_suppresses_questionable_days_not_share_history(client, session):
    days = [date(2026, 1, d) for d in (5, 6, 7, 8)]
    code = '159915.XSHE'
    for day, shares, close in zip(days, (100, 300, 300, 300), ('3', '3', '1', '1')):
        session.add_all([row(day, shares, code), price(day, close, code)])
    session.commit()
    result = client.get('/api/v1/etf-shares/history?group=cyb').json()
    points = {p['date']: p for p in result['market_values']}
    for day in days[1:3]:
        assert points[str(day)]['market_value'] is None
        assert points[str(day)]['excluded_funds'] == [code]
        assert points[str(day)]['quality_flags']
    assert Decimal(points[str(days[3])]['market_value']) == 300
    assert [p['shares'] for p in result['funds'][0]['points']] == ['100', '300', '300', '300']


def test_reverse_split_and_large_creation_are_both_conservatively_excluded(client, session):
    days = [date(2026, 1, d) for d in (5, 6, 7, 8)]
    code = '510180.XSHG'
    for day, shares, close in zip(days, (400, 100, 100, 100), ('1', '1', '4', '4')):
        session.add_all([row(day, shares, code), price(day, close, code)])
    session.commit()
    points = client.get('/api/v1/etf-shares/history?group=sz180').json()['market_values']
    assert points[1]['market_value'] is None and points[2]['market_value'] is None
    assert Decimal(points[3]['market_value']) == 400
    # A genuine large creation is intentionally withheld too, not mislabeled confirmed split.
    session.get(ETFPrice, (code, days[2])).close_price = Decimal(1)
    session.get(ETFPrice, (code, days[3])).close_price = Decimal(1)
    session.commit()
    assert client.get('/api/v1/etf-shares/history?group=sz180').json()['market_values'][1]['market_value'] is None


def test_price_and_shares_outside_chart_range_still_screen_first_visible_day(client, session):
    before, start, end = date(2026, 1, 5), date(2026, 1, 6), date(2026, 2, 4)
    code = '159915.XSHE'
    session.add_all([row(before, 100, code), price(before, '3', code),
                     row(start, 300, code, previous=before), price(start, '3', code),
                     row(end, 300, code), price(end, '1', code)])
    session.commit()
    result = client.get('/api/v1/etf-shares/history?group=cyb&days=30').json()
    assert result['start_date'] == str(start)
    assert result['market_values'][0]['market_value'] is None
    assert result['market_values'][0]['quality_flags'] == ['possible_share_unit_change']


def test_persisted_exchange_calendar_breaks_pre_2026_missing_days(client, session):
    from app.models.etf import ETFTradingDay
    days = [date(2025, 12, d) for d in (29, 30, 31)]
    code = '159915.XSHE'
    session.add_all([row(days[0], 100, code), price(days[0], '1', code),
                     row(days[2], 100, code), price(days[2], '1', code)])
    session.add(ETFTradingDay(trade_date=days[1], previous_trade_date=days[0],
        source='SSE.INDEX_TRADING_DAYS', updated_at=datetime(2026, 1, 8)))
    session.commit()
    result = client.get('/api/v1/etf-shares/history?group=cyb').json()
    assert [p['date'] for p in result['market_values']] == [str(d) for d in days]
    assert result['market_values'][1]['market_value'] is None
    assert result['observed_days'] == 2


def test_quality_flags_are_stable_when_chart_starts_after_share_change(client, session):
    code = '159915.XSHE'
    before, changed, first, end = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 2, 5)
    for day, shares in ((before, 100), (changed, 200), (first, 200), (end, 200)):
        session.add_all([row(day, shares, code), price(day, '1', code)])
    session.commit()
    full = client.get('/api/v1/etf-shares/history?group=cyb&days=0').json()
    narrow = client.get('/api/v1/etf-shares/history?group=cyb&days=30').json()
    by_day = lambda result: {p['date']: p for p in result['market_values']}
    assert narrow['start_date'] == str(first)
    assert by_day(narrow)[str(first)] == by_day(full)[str(first)]
    assert by_day(narrow)[str(first)]['market_value'] is None


def test_known_next_day_price_change_screens_historical_end_date(client, session):
    code = '159915.XSHE'
    days = [date(2026, 1, d) for d in (5, 6, 7)]
    for day, close in zip(days, ('3', '3', '1')):
        session.add_all([row(day, 100, code), price(day, close, code)])
    session.commit()
    full = client.get('/api/v1/etf-shares/history?group=cyb&days=0').json()
    historical = client.get('/api/v1/etf-shares/history?group=cyb&days=0&end_date=2026-01-06').json()
    full_day = next(p for p in full['market_values'] if p['date'] == str(days[1]))
    assert historical['market_values'][-1] == full_day
    assert full_day['market_value'] is None
    assert historical['funds'][0]['date'] == '2026-01-06'
