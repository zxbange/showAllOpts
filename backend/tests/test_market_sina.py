"""Offline provider and aggregation contracts; no live market API requests."""
import ast
import builtins
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from app.services.market_methodology import JOINQUANT_HEAT_METHOD, LEGACY_SINA_HEAT_METHOD
from app.services.market_sina import (
    COUNT_URL, HQ_BATCH_SIZE, HQ_URL, KLINE_URL, UNIVERSE_URL, SinaDataError, SinaMarketProvider,
    SinaRateLimitError, aggregate_crowding, aggregate_heat, calculate_joinquant_quote,
)


DAY = date(2026, 11, 10)
SESSIONS = [value.date() for value in pd.bdate_range(end=DAY, periods=20)]


def stock(symbol, *, close='10.00', volume='100', amount='1000'):
    return {'symbol': symbol, 'code': symbol[2:], 'name': '测试股票',
            'trade': close, 'volume': volume, 'amount': amount,
            'ticktime': '15:00:00', 'source_extra': 'preserved'}


def snapshot_row(symbol, *, close='10.00', volume='100', amount='1000'):
    return {'symbol': symbol, 'name': '测试股票', 'close': close,
            'volume': volume, 'amount': amount, 'ticktime': '15:00:00'}


def snapshot(rows):
    return {'date': str(DAY), 'expected_count': len(rows), 'rows': rows}


def bar(day=DAY, *, close='10.00', volume='100', ma='9.00'):
    result = {'day': str(day), 'open': '9.50', 'high': '11.00', 'low': '9.00',
              'close': close, 'volume': volume}
    if ma is not None:
        result['ma_price20'] = ma
    return result


def observation(symbol, *, status='current', close='10.00', volume='100', ma='9.00',
                actual=DAY, history_count=None):
    return {'symbol': symbol, 'day': str(DAY), 'date': str(actual), 'close': close,
            'volume': volume, 'ma20': ma, 'status': status, 'history_count': history_count}


def qfq_arguments(symbol='sh600519', *, values=None, listed_from=None, pauses=None):
    """Explicitly synthetic verified inputs; no live source gains these flags."""
    values = values if values is not None else ['9'] * 19 + ['10']
    pauses = pauses or {}
    listed_from = listed_from or date(2000, 1, 1)
    rows = [{'date': str(day), 'close_qfq': close, 'paused': pauses.get(day, 0)}
            for day, close in zip(SESSIONS, values) if day >= listed_from]
    volume = '0' if pauses.get(DAY) == 1 else '100'
    rows[-1].update(raw_close='10', volume=volume)
    return {
        'calendar': SESSIONS, 'rows': rows, 'listed_from': listed_from,
        'listed_until': date(2099, 12, 31),
        'evidence': {**{key: True for key in ('calendar_verified', 'listing_verified',
                                             'paused_verified', 'prices_verified', 'universe_verified')},
                     'price_basis': 'qfq', 'adjustment_basis': 'synthetic-common-basis'},
        'snapshot': snapshot_row(symbol, volume=volume, amount='0' if volume == '0' else '1000'),
    }


def verified_quote(symbol='sh600519', **kwargs):
    return calculate_joinquant_quote(symbol, DAY, **qfq_arguments(symbol, **kwargs))


def original_function(filename, name):
    """Load only the pure original function, never collector imports/config."""
    path = Path(__file__).resolve().parents[2] / 'collectors' / filename
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {'np': np, 'pd': pd, 'as_date': lambda value: value,
                 'Decimal': Decimal, 'InvalidOperation': InvalidOperation,
                 'builtins': builtins, 'TOP_PCT': Decimal('0.05')}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


def hq_assignment(symbol, *, actual=DAY, close='10.00', volume='100', amount='1000.000',
                  state='00', extra=1):
    fields = ['0'] * (33 + extra)
    fields[0], fields[1], fields[2], fields[3] = '测试股票', '9.90', '9.90', close
    fields[8], fields[9] = volume, amount
    fields[30], fields[31], fields[32] = str(actual), '15:00:03', state
    return f'var hq_str_{symbol}=' + json.dumps(','.join(fields), ensure_ascii=False) + ';\n'


def jsonp(payload):
    return httpx.Response(200, text='/*<script>inert comment</script>*/\n=(' + json.dumps(payload) + ');')


def test_universe_exact_page_boundary_and_three_markets_keep_every_source_field():
    rows = [stock('bj920599'), stock('sz302132')]
    rows += [stock(f'sh{600000 + number:06}') for number in range(78)]
    calls = []

    def serve(request):
        calls.append(request)
        assert request.url.scheme == 'https'
        if request.url.path.endswith('getHQNodeStockCount'):
            return httpx.Response(200, json='80')
        assert request.url.params['node'] == 'hs_a'
        assert request.url.params['num'] == '80'
        assert request.url.params['page'] == '1'
        return httpx.Response(200, json=rows)

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        with SinaMarketProvider(client) as provider:
            result = provider.fetch_universe(DAY)
        assert not client.is_closed  # An injected client belongs to the worker.
    assert len(calls) == 3  # Count, exactly one page, count; no empty extra page.
    assert result['expected_count'] == len(result['rows']) == 80
    assert {row['symbol'] for row in result['rows']} == {row['symbol'] for row in rows}
    assert result['rows'][0]['raw']['source_extra'] == 'preserved'
    assert result['rows'][0]['volume'] == '100'
    assert result['source_has_trading_date'] is False
    assert result['raw']['count_before'] == result['raw']['count_after'] == '80'
    assert len(result['raw']['requests']) == 3
    json.dumps(result)  # Raw and normalized responses can be persisted directly.


@pytest.mark.parametrize('failure', ['short_page', 'duplicate', 'count_change', 'missing_market'])
def test_incomplete_or_mutating_universe_cannot_publish(failure):
    rows = [stock('bj920599'), stock('sz302132'), stock('sh600519')]
    calls = []

    def serve(request):
        calls.append(request)
        if request.url.path.endswith('getHQNodeStockCount'):
            count = '4' if failure == 'count_change' and len(calls) > 1 else '3'
            return httpx.Response(200, json=count)
        changed = list(rows)
        if failure == 'short_page':
            changed.pop()
        elif failure == 'duplicate':
            changed[-1] = changed[0]
        elif failure == 'missing_market':
            changed[0] = stock('sh600000')
        return httpx.Response(200, json=changed)

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(SinaDataError):
            SinaMarketProvider(client).fetch_universe(DAY)


def test_duplicate_across_pages_is_detected_even_when_counts_match():
    first = [stock('bj920599'), stock('sz302132')]
    first += [stock(f'sh{600000 + number:06}') for number in range(78)]

    def serve(request):
        if request.url.path.endswith('getHQNodeStockCount'):
            return httpx.Response(200, json='81')
        return httpx.Response(200, json=first if request.url.params['page'] == '1' else [first[0]])

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(SinaDataError, match='重复'):
            SinaMarketProvider(client).fetch_universe(DAY)


@pytest.mark.parametrize('field,bad', [
    ('trade', '-1'), ('trade', True), ('volume', '0.5'), ('amount', 'NaN'),
    ('amount', 'Infinity'), ('amount', None), ('amount', '1e999999'),
    ('symbol', 'sh600519<script>'), ('code', '000001'),
])
def test_invalid_universe_values_fail_closed(field, bad):
    rows = [stock('bj920599'), stock('sz302132'), stock('sh600519')]
    rows[-1][field] = bad

    def serve(request):
        return httpx.Response(200, json='3' if request.url.path.endswith('getHQNodeStockCount') else rows)

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(SinaDataError):
            SinaMarketProvider(client).fetch_universe(DAY)


def test_quote_uses_provider_ma20_and_decimal_exact_close_volume_matches():
    calls = []

    def serve(request):
        calls.append(request)
        assert str(request.url).startswith(KLINE_URL)
        assert dict(request.url.params) == {
            'symbol': 'sh600519', 'scale': '240', 'ma': '20', 'datalen': '1',
        }
        return jsonp([bar(close='1439.000', volume='2303784', ma=1374.261)])

    row = snapshot_row('sh600519', close='1439', volume=2303784)
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY, snapshot=row)
    assert result['status'] == 'current'
    assert result['ma20'] == '1374.261'
    assert result['close'] == '1439.000'
    assert result['date'] == result['day'] == '2026-11-10'
    assert result['adjustment'] == 'unadjusted'
    assert result['methodology'] == LEGACY_SINA_HEAT_METHOD
    assert result['verified'] is False
    assert len(calls) == 1
    assert result['raw']['requests'][0]['text'].startswith('/*')
    json.dumps(result)


@pytest.mark.parametrize('changed', [
    {'close': '10.01'}, {'volume': '101'}, {'symbol': 'sz000001'},
])
def test_current_quote_disagreement_is_pending_not_not_above(changed):
    row = snapshot_row('sh600519')
    row.update(changed)
    with httpx.Client(transport=httpx.MockTransport(lambda request: jsonp([bar()]))) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY, snapshot=row)
    assert result['status'] == 'mismatch'
    assert result['error'] == 'snapshot_quote_mismatch'


@pytest.mark.parametrize('ma', ['0', '-1', 'NaN', 'Infinity', True, 'invalid'])
def test_invalid_ma_never_becomes_zero_or_new_stock(ma):
    calls = []

    def serve(request):
        calls.append(request)
        return jsonp([bar(ma=ma)])

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY)
    assert result['status'] == 'transient_failure'
    assert result['error'] == 'invalid_number'
    assert len(calls) == 1


@pytest.mark.parametrize('length,status', [(1, 'insufficient_history'), (19, 'insufficient_history'),
                                           (20, 'unknown_missing_ma')])
def test_missing_ma_requires_twenty_record_confirmation(length, status):
    calls = []

    def serve(request):
        calls.append(request)
        requested = int(request.url.params['datalen'])
        size = 1 if requested == 1 else length
        return jsonp([bar(DAY - timedelta(days=i), ma=None) for i in reversed(range(size))])

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote(
            'sh600519', DAY, snapshot=snapshot_row('sh600519'))
    assert result['status'] == status
    assert result['ma20'] is None
    assert result['history_count'] == length
    assert [request.url.params['datalen'] for request in calls] == ['1', '20']


def test_missing_ma_confirmation_failure_stays_pending_and_retains_both_raw_responses():
    calls = []

    def serve(request):
        calls.append(request)
        return jsonp([bar(ma=None)]) if len(calls) == 1 else httpx.Response(503, text='unavailable')

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY)
    assert result['status'] == 'transient_failure'
    assert result['error'] == 'http_error'
    assert result['history_count'] is None
    assert len(result['raw']['requests']) == 2


def test_ma_becomes_available_in_second_response_without_local_recalculation():
    def serve(request):
        return jsonp([bar(ma=None if request.url.params['datalen'] == '1' else '77.123')])

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY)
    assert result['status'] == 'current'
    assert result['ma20'] == '77.123'  # Deliberately unlike any average of these closes.


@pytest.mark.parametrize('amount,volume,expected', [
    ('0', '0', 'no_trade'), ('1', '0', 'stale'), ('0', '1', 'stale'), ('1', '1', 'stale'),
])
def test_old_bar_only_confirms_no_trade_with_both_snapshot_zeroes(amount, volume, expected):
    row = snapshot_row('sh600519', amount=amount, volume=volume)
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: jsonp([bar(DAY - timedelta(days=1))]))) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY, snapshot=row)
    assert result['status'] == expected
    assert result['date'] == '2026-11-09'
    assert result['day'] == '2026-11-10'


def test_no_snapshot_or_empty_response_cannot_confirm_no_trade():
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: jsonp([bar(DAY - timedelta(days=1))]))) as client:
        assert SinaMarketProvider(client).fetch_quote('sh600519', DAY)['status'] == 'stale'
    with httpx.Client(transport=httpx.MockTransport(lambda request: jsonp([]))) as client:
        result = SinaMarketProvider(client).fetch_quote(
            'sh600519', DAY, snapshot=snapshot_row('sh600519', volume='0', amount='0'))
    assert result['status'] == 'transient_failure'


def test_historical_retry_selects_exact_day_and_does_not_substitute_future_bar():
    payload = [bar(DAY - timedelta(days=1), ma='8'), bar(ma='9'),
               bar(DAY + timedelta(days=1), close='500', ma='450')]
    with httpx.Client(transport=httpx.MockTransport(lambda request: jsonp(payload))) as client:
        result = SinaMarketProvider(client).fetch_quote(
            'sh600519', DAY, snapshot=snapshot_row('sh600519'), datalen=1970)
    assert (result['date'], result['close'], result['ma20'], result['status']) == (
        '2026-11-10', '10.00', '9', 'current')
    with httpx.Client(transport=httpx.MockTransport(lambda request: jsonp(payload[2:]))) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY, datalen=1970)
    assert result['status'] == 'transient_failure'
    assert result['date'] is None


def test_full_new_stock_history_counts_only_records_through_historical_target():
    # The stock now has 33 bars, but only five existed on the requested day.
    target = DAY - timedelta(days=28)
    payload = [bar(DAY - timedelta(days=i), ma=None) for i in reversed(range(33))]
    calls = []

    def serve(request):
        calls.append(request)
        return jsonp(payload)

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote(
            'sh600519', target, snapshot=snapshot_row('sh600519'), datalen=1970)
    assert result['status'] == 'insufficient_history'
    assert result['history_count'] == 5
    assert result['history_returned'] == 33
    assert result['history_requested'] == 1970
    assert result['history_complete'] is True
    assert len(calls) == 1


@pytest.mark.parametrize('available_count,expected_count,status', [
    (33, 16, 'insufficient_history'), (60, 23, 'unknown_missing_ma'),
])
def test_historical_target_near_full_window_start_requires_bounded_expansion(
        available_count, expected_count, status):
    target = DAY - timedelta(days=17)
    all_bars = [bar(DAY - timedelta(days=i), ma=None) for i in reversed(range(available_count))]
    calls = []

    def serve(request):
        requested = int(request.url.params['datalen'])
        calls.append(requested)
        return jsonp(all_bars[-requested:])

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote(
            'sh600519', target, snapshot=snapshot_row('sh600519'), datalen=20)
    # The first 20-bar window has only three bars through target. That alone is
    # not evidence of an IPO; extending the window reaches 16 or 23 past bars.
    assert calls == [20, 40]
    assert result['status'] == status
    assert result['history_count'] == expected_count
    assert result['history_complete'] is (available_count < 40)


def test_full_maximum_window_cannot_prove_insufficient_history():
    target = DAY - timedelta(days=1965)
    payload = [bar(DAY - timedelta(days=i), ma=None) for i in reversed(range(1970))]
    calls = []

    def serve(request):
        calls.append(request)
        return jsonp(payload)

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_quote(
            'sh600519', target, snapshot=snapshot_row('sh600519'), datalen=1970)
    assert result['history_count'] == 5
    assert result['history_complete'] is False
    assert result['status'] == 'unknown_missing_ma'
    assert len(calls) == 1  # Never exceed the endpoint limit or retry forever.


@pytest.mark.parametrize('text', ['<html>verification</html>', '=(null);',
                                 '=([]); console.log("unexpected");', '=(NaN);'])
def test_non_json_or_script_response_is_never_executed_or_accepted(text):
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=text))) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY)
    assert result['status'] == 'transient_failure'


def test_duplicate_bar_dates_and_zero_current_volume_remain_unresolved():
    with httpx.Client(transport=httpx.MockTransport(lambda request: jsonp([bar(), bar()]))) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY)
    assert result['error'] == 'duplicate_date'
    with httpx.Client(transport=httpx.MockTransport(lambda request: jsonp([bar(volume='0')]))) as client:
        result = SinaMarketProvider(client).fetch_quote(
            'sh600519', DAY, snapshot=snapshot_row('sh600519', volume='0', amount='0'))
    assert result['status'] == 'mismatch'


@pytest.mark.parametrize('status_code', [403, 429])
@pytest.mark.parametrize('operation', ['universe', 'quote', 'latest_session', 'dated_snapshot'])
def test_access_or_rate_limit_interrupts_batch_without_retry_or_host_switch(status_code, operation):
    calls = []

    def serve(request):
        calls.append(request)
        return httpx.Response(status_code, headers={'Retry-After': '60'})

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        provider = SinaMarketProvider(client)
        with pytest.raises(SinaRateLimitError) as caught:
            if operation == 'universe':
                provider.fetch_universe(DAY)
            elif operation == 'quote':
                provider.fetch_quote('sh600519', DAY)
            elif operation == 'dated_snapshot':
                provider.fetch_dated_snapshot(snapshot([snapshot_row('sh600519')]), DAY)
            else:
                provider.latest_session_day()
    assert caught.value.status_code == status_code
    assert caught.value.retry_after == 60
    assert caught.value.is_rate_limited
    assert len(calls) == 1


def test_transport_failure_is_single_attempt_and_worker_cancellation_propagates():
    calls = []

    def unavailable(request):
        calls.append(request)
        raise httpx.ReadTimeout('temporary', request=request)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        result = SinaMarketProvider(client).fetch_quote('sh600519', DAY)
    assert result['status'] == 'transient_failure'
    assert result['error'] == 'network_error'
    assert len(calls) == 1

    def cancelled(request):
        raise RuntimeError('worker cancelled')

    with httpx.Client(transport=httpx.MockTransport(cancelled)) as client:
        with pytest.raises(RuntimeError, match='worker cancelled'):
            SinaMarketProvider(client).fetch_quote('sh600519', DAY)


def test_latest_session_is_actual_index_day_and_stale_index_is_not_a_holiday_decision():
    def serve(request):
        assert request.url.params['symbol'] == 'sh000001'
        assert request.url.params['scale'] == '240'
        assert request.url.params['ma'] == 'no'
        assert request.url.params['datalen'] == '1'
        return jsonp([bar(DAY - timedelta(days=1), ma=None)])

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        assert SinaMarketProvider(client).latest_session_day() == date(2026, 11, 9)


def test_heat_uses_original_eligible_denominator_and_excludes_proved_pauses_and_ipos():
    rows = [snapshot_row('sh600519'), snapshot_row('sz000001'), snapshot_row('bj920599'),
            snapshot_row('sh688053', volume='0', amount='0')]
    quotes = [verified_quote('sh600519'), verified_quote('sz000001', values=['10'] * 20),
              verified_quote('bj920599', listed_from=SESSIONS[-4]),
              verified_quote('sh688053', values=['10'] * 20, pauses={DAY: 1})]
    result = aggregate_heat(snapshot(rows), quotes)
    assert result['publishable']
    assert result['above_count'] == 1
    assert result['universe_count'] == 4
    assert result['valid_count'] == result['eligible_count'] == 2
    assert result['insufficient_history_count'] == result['paused_count'] == 1
    assert result['ratio'] == result['value'] == 0.5
    assert result['data_quality']['pending_count'] == 0
    assert result['methodology'] == JOINQUANT_HEAT_METHOD
    assert result['price_basis'] == 'qfq'
    assert result['denominator'] == 'eligible'


@pytest.mark.parametrize('problem', [
    'missing', 'unknown_missing_ma', 'stale', 'transient_failure', 'mismatch',
    'duplicate', 'different_day', 'different_close', 'different_volume', 'invalid_ma',
    'unconfirmed_new_stock', 'extra_stock', 'unconfirmed_no_trade',
    'legacy_method', 'unverified', 'wrong_basis', 'wrong_divisor', 'missing_evidence',
    'paused_unknown', 'incorrect_above',
])
def test_unexpected_missing_or_mismatched_heat_prevents_publication(problem):
    rows = [snapshot_row('sh600519')]
    quotes = [verified_quote('sh600519')]
    if problem == 'missing':
        quotes = []
    elif problem in ('unknown_missing_ma', 'stale', 'transient_failure', 'mismatch'):
        quotes[0]['status'] = problem
    elif problem == 'duplicate':
        quotes.append(dict(quotes[0]))
    elif problem == 'different_day':
        quotes[0]['date'] = '2026-11-09'
    elif problem == 'different_close':
        quotes[0]['close'] = '10.01'
    elif problem == 'different_volume':
        quotes[0]['volume'] = '101'
    elif problem == 'invalid_ma':
        quotes[0]['ma20'] = None
    elif problem == 'unconfirmed_new_stock':
        quotes[0].update(status='insufficient_history', ma20=None, history_count=20)
    elif problem == 'extra_stock':
        quotes.append(verified_quote('sz000001'))
    elif problem == 'unconfirmed_no_trade':
        quotes[0].update(status='no_trade', date='2026-11-09')
    elif problem == 'legacy_method':
        quotes[0]['methodology'] = LEGACY_SINA_HEAT_METHOD
    elif problem == 'unverified':
        quotes[0]['verified'] = False
    elif problem == 'wrong_basis':
        quotes[0]['price_basis'] = 'bfq'
    elif problem == 'wrong_divisor':
        quotes[0]['denominator'] = 'universe'
    elif problem == 'missing_evidence':
        quotes[0]['evidence'].pop('paused_verified')
    elif problem == 'paused_unknown':
        quotes[0]['paused'] = None
    elif problem == 'incorrect_above':
        quotes[0]['above'] = False
    result = aggregate_heat(snapshot(rows), quotes)
    assert not result['publishable']
    assert result['ratio'] is None and result['value'] is None
    assert result['pending_count'] or result['unexpected_symbols']


def test_crowding_counts_zero_amount_rows_for_top_floor_and_stable_decimal_ranking():
    rows = [snapshot_row(f'sh{600000 + i:06}', amount='0') for i in range(119)]
    for index in range(6):
        rows[index]['amount'] = f'10000000000000000.0{index + 1}'
    result = aggregate_crowding(snapshot(rows))
    assert result['stock_count'] == result['universe_count'] == 119
    assert result['top_stock_count'] == result['top_count'] == 5
    assert result['top_symbols'] == [f'sh60000{number}' for number in range(5, 0, -1)]
    assert Decimal(result['top_money']) == Decimal('50000000000000000.20')
    assert Decimal(result['total_money']) == Decimal('60000000000000000.21')
    assert result['ratio'] == float((Decimal(result['top_money']) / Decimal(result['total_money'])).quantize(
        Decimal('0.0000000001')))
    # A tie is resolved by code, independently of input/page order.
    rows[0]['amount'] = rows[1]['amount']
    assert aggregate_crowding(snapshot(list(reversed(rows))))['top_symbols'][-1] == 'sh600000'


def test_zero_total_crowding_and_invalid_or_empty_universe_do_not_produce_a_false_zero():
    result = aggregate_crowding(snapshot([snapshot_row('sh600519', amount='0')]))
    assert not result['publishable']
    assert result['ratio'] is None
    assert result['error'] == 'zero_total_amount'
    with pytest.raises(SinaDataError):
        aggregate_crowding([])
    with pytest.raises(SinaDataError):
        aggregate_crowding([snapshot_row('sh600519', amount='-1')])
    with pytest.raises(SinaDataError):
        aggregate_heat({'date': str(DAY), 'expected_count': 2,
                        'rows': [snapshot_row('sh600519')]}, [])


def test_live_heat_capability_stays_blocked_and_cannot_relabel_raw_api_ma():
    capability = SinaMarketProvider.heat_capability()
    assert capability['methodology'] == JOINQUANT_HEAT_METHOD
    assert capability['verified'] is False
    assert capability['error'] == 'heat_methodology_unverified'
    assert set(capability['missing_evidence']) == {'historical_paused', 'listing_dates', 'qfq_ma20_window'}
    with httpx.Client(transport=httpx.MockTransport(lambda request: jsonp([bar()]))) as client:
        quote = SinaMarketProvider(client).fetch_quote('sh600519', DAY)
    result = aggregate_heat(snapshot([snapshot_row('sh600519')]), [quote])
    assert result['pending_count'] == 1
    assert result['publishable'] is False
    assert result['ratio'] is None


@pytest.mark.parametrize('scenario', [
    'above', 'equal', 'below_tolerance', 'above_tolerance', 'historical_pause',
    'current_pause', 'ipo_four_sessions',
])
def test_verified_window_matches_original_joinquant_oracle(scenario):
    args = qfq_arguments()
    if scenario in ('equal', 'below_tolerance', 'above_tolerance', 'current_pause'):
        for row in args['rows']:
            row['close_qfq'] = '10'
    if scenario == 'below_tolerance':
        args['rows'][-1]['close_qfq'] = '10.000000000005'
    elif scenario == 'above_tolerance':
        args['rows'][-1]['close_qfq'] = '10.00000000005'
    elif scenario == 'historical_pause':
        args['rows'][5].update(paused=1, close_qfq=None)
        args['rows'][6].update(paused=1, close_qfq=None)
    elif scenario == 'current_pause':
        args['rows'][-1].update(paused=1, volume='0')
        args['snapshot'].update(volume='0', amount='0')
    elif scenario == 'ipo_four_sessions':
        args['listed_from'] = SESSIONS[-4]
        args['rows'] = args['rows'][-4:]
    quote = calculate_joinquant_quote('sh600519', DAY, **args)
    # The real collector consumes a filled close on an explicit paused date.
    frame_rows, previous = [], None
    for row in args['rows']:
        value = row['close_qfq'] if row['close_qfq'] is not None else previous
        frame_rows.append({'code': 'sh600519', 'time': date.fromisoformat(row['date']),
                           'close': float(value), 'paused': row['paused']})
        previous = value
    oracle = original_function('市场情绪跟踪_数据库版.py', 'batch_counts')
    expected = oracle(pd.DataFrame(frame_rows), ['sh600519'], SESSIONS,
                      {'sh600519': (args['listed_from'], args['listed_until'])})[-1]
    result = aggregate_heat(snapshot([args['snapshot']]), [quote])
    assert [result['universe_count'], result['eligible_count'], result['above_count']] == list(expected)
    assert result['pending_count'] == 0
    assert result['publishable'] is bool(expected[1] > 0)
    if expected[1] == 0:
        assert result['ratio'] is None
        assert result['error'] == 'zero_eligible_count'
    if scenario == 'historical_pause':
        assert quote['raw']['filled_paused_dates'] == [str(SESSIONS[5]), str(SESSIONS[6])]
        assert quote['history_count'] == 20  # Pauses do not drop market sessions.
    json.dumps(quote)


def test_qfq_exdiv_numerical_fixture_retains_full_precision_not_bfq_api_ma():
    # Public 000686 closes/factor from 2026-11-10; pause/listing evidence here is
    # explicitly synthetic. This establishes the arithmetic, not live capability.
    closes = '7.62 7.57 7.45 7.48 7.41 7.33 7.26 7.32 7.32 7.12 7.05 6.98 6.90 7.16 7.20 7.28 7.22 7.18 7.19'.split()
    values = [str(Decimal(value) / Decimal('1.0141043723554000')) for value in closes] + ['7.09']
    args = qfq_arguments('sz000686', values=values)
    args['rows'][-1]['raw_close'] = args['snapshot']['close'] = '7.09'
    quote = calculate_joinquant_quote('sz000686', DAY, **args)
    assert float(quote['ma20']) == 7.160505563282539
    assert quote['ma20'] != '7.257'  # The unadjusted API MA20 is a different basis.
    assert quote['above'] is False


@pytest.mark.parametrize('problem', [
    'calendar_short', 'calendar_duplicate', 'calendar_unsorted', 'calendar_wrong_target',
    'row_missing', 'row_duplicate', 'row_future', 'unknown_pause', 'boolean_pause',
    'untraded_not_pause', 'paused_first_without_price', 'paused_price_changed',
    'missing_listing_evidence', 'missing_price_evidence', 'missing_target_universe_evidence',
    'bfq_evidence', 'mixed_adjustment_basis', 'listed_later', 'delisted_earlier',
    'different_snapshot_close', 'different_snapshot_volume', 'different_snapshot_date',
])
def test_pure_heat_calculator_rejects_unproven_inputs_instead_of_shrinking_divisor(problem):
    args = qfq_arguments()
    if problem == 'calendar_short':
        args['calendar'] = SESSIONS[1:]
    elif problem == 'calendar_duplicate':
        args['calendar'] = [SESSIONS[1], *SESSIONS[1:]]
    elif problem == 'calendar_unsorted':
        args['calendar'] = [SESSIONS[1], SESSIONS[0], *SESSIONS[2:]]
    elif problem == 'calendar_wrong_target':
        args['calendar'] = [*SESSIONS[:-1], DAY + timedelta(days=1)]
    elif problem == 'row_missing':
        args['rows'].pop(5)
    elif problem == 'row_duplicate':
        args['rows'].append(deepcopy(args['rows'][0]))
    elif problem == 'row_future':
        args['rows'][0]['date'] = str(DAY + timedelta(days=1))
    elif problem == 'unknown_pause':
        args['rows'][5]['paused'] = None
    elif problem == 'boolean_pause':
        args['rows'][5]['paused'] = True
    elif problem == 'untraded_not_pause':
        args['rows'][5]['close_qfq'] = None
    elif problem == 'paused_first_without_price':
        args['rows'][0].update(paused=1, close_qfq=None)
    elif problem == 'paused_price_changed':
        args['rows'][5].update(paused=1, close_qfq='8')
    elif problem == 'missing_listing_evidence':
        args['evidence']['listing_verified'] = False
    elif problem == 'missing_price_evidence':
        args['evidence']['prices_verified'] = False
    elif problem == 'missing_target_universe_evidence':
        args['evidence']['universe_verified'] = False
    elif problem == 'bfq_evidence':
        args['evidence']['price_basis'] = 'bfq'
    elif problem == 'mixed_adjustment_basis':
        args['evidence']['adjustment_basis'] = ''
    elif problem == 'listed_later':
        args['listed_from'] = DAY + timedelta(days=1)
    elif problem == 'delisted_earlier':
        args['listed_until'] = DAY - timedelta(days=1)
    elif problem == 'different_snapshot_close':
        args['snapshot']['close'] = '10.01'
    elif problem == 'different_snapshot_volume':
        args['snapshot']['volume'] = '101'
    elif problem == 'different_snapshot_date':
        args['snapshot']['date'] = '2026-11-09'
    with pytest.raises(SinaDataError):
        calculate_joinquant_quote('sh600519', DAY, **args)


def test_heat_ratio_matches_original_ten_decimal_storage_precision():
    symbols = ['sh600519', 'sz000001', 'bj920599']
    quotes = [verified_quote(symbol, values=['10'] * 20) for symbol in symbols]
    quotes[0] = verified_quote(symbols[0])
    result = aggregate_heat(snapshot([snapshot_row(symbol) for symbol in symbols]), quotes)
    assert result['ratio'] == 0.3333333333


def test_crowding_matches_original_collector_money_floor_and_precision():
    rows = [snapshot_row(f'sh{600000 + i:06}', amount=str(Decimal(i) * Decimal('1234.567')))
            for i in range(119)]
    oracle = original_function('抱团拥挤度_数据库版.py', 'summarize_money')
    expected = oracle([(row['symbol'], row['amount']) for row in rows])
    actual = aggregate_crowding(snapshot(rows))
    assert actual['publishable'] is True
    for key in ('top_money', 'total_money', 'stock_count', 'top_stock_count'):
        assert Decimal(str(actual[key])) == expected[key]
    assert actual['ratio'] == float(expected['ratio'])
    small = aggregate_crowding(snapshot(rows[:99]))
    assert small['publishable'] is False
    assert small['error'] == 'insufficient_universe'
    assert small['ratio'] is None


def test_dated_snapshot_uses_literal_comma_batches_and_preserves_exact_amount_and_raw():
    rows = [snapshot_row('bj920599'), snapshot_row('sz000001')]
    rows += [snapshot_row(f'sh{600000 + index:06}') for index in range(79)]
    rows[0]['amount'] = '1001'  # Half-up .500 yuan must round upwards.
    original, calls = snapshot(rows), []

    def serve(request):
        calls.append(request)
        assert str(request.url).startswith(HQ_URL)
        assert '%2C' not in str(request.url)
        assert request.headers['Referer'] == 'https://finance.sina.com.cn/'
        symbols = request.url.params['list'].split(',')
        assert len(symbols) <= HQ_BATCH_SIZE
        return httpx.Response(200, text=''.join(hq_assignment(
            symbol, amount='1000.500' if symbol == 'bj920599' else '1000.490',
            extra=6 if symbol.startswith('bj') else 1) for symbol in symbols))

    checkpoints = []
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = SinaMarketProvider(client).fetch_dated_snapshot(original, DAY, checkpoint=checkpoints.append)
    assert [len(batch) for batch in checkpoints] == [40, 40, 1]
    assert len(calls) == 3
    assert original['rows'][0]['amount'] == '1001'  # Caller input remains unchanged.
    assert result['rows'][0]['amount'] == '1000.500'
    assert result['rows'][0]['node_amount'] == '1001'
    assert result['source_has_trading_date'] is result['dated_snapshot_verified'] is True
    assert result['date_verified_by'] == 'sina_hq'
    assert all(row['date'] == str(DAY) for row in result['rows'])
    assert len(result['raw']['hq_requests']) == 3
    assert len(result['rows'][0]['hq_raw']) == 39
    json.dumps(result)


@pytest.mark.parametrize('state,volume,amount,expected', [
    ('00', '100', '1000', 0), ('00', '0', '0', None),
    ('02', '0', '0', 1), ('03', '0', '0', 1), ('03', '100', '1000', None),
])
def test_hq_status_does_not_turn_zero_volume_normal_state_into_verified_pause(state, volume, amount, expected):
    row = snapshot_row('sh688053', volume=volume, amount=amount)
    payload = hq_assignment(row['symbol'], volume=volume, amount=amount, state=state)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=payload))) as client:
        result = SinaMarketProvider(client).fetch_dated_snapshot(snapshot([row]), DAY)
    assert result['rows'][0]['hq_state'] == state
    assert result['rows'][0]['paused'] == expected
    assert SinaMarketProvider.heat_capability()['verified'] is False


@pytest.mark.parametrize('problem', ['old_date', 'missing_symbol', 'extra_symbol', 'duplicate_symbol',
                                     'price_mismatch', 'volume_mismatch', 'amount_mismatch',
                                     'fractional_volume', 'negative_amount', 'trailing_code', 'empty_fields'])
def test_dated_snapshot_rejects_incomplete_or_mismatched_raw_before_checkpoint(problem):
    kwargs = {}
    if problem == 'old_date':
        kwargs['actual'] = DAY - timedelta(days=1)
    elif problem == 'price_mismatch':
        kwargs['close'] = '10.01'
    elif problem == 'volume_mismatch':
        kwargs['volume'] = '101'
    elif problem == 'amount_mismatch':
        kwargs['amount'] = '1000.51'
    elif problem == 'fractional_volume':
        kwargs['volume'] = '100.1'
    elif problem == 'negative_amount':
        kwargs['amount'] = '-1'
    payload = hq_assignment('sh600519', **kwargs)
    if problem == 'missing_symbol':
        payload = ''
    elif problem == 'extra_symbol':
        payload += hq_assignment('sz000001')
    elif problem == 'duplicate_symbol':
        payload += payload
    elif problem == 'trailing_code':
        payload += 'throw new Error("must not execute");'
    elif problem == 'empty_fields':
        payload = 'var hq_str_sh600519="";'
    checkpoints = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=payload))) as client:
        with pytest.raises(SinaDataError):
            SinaMarketProvider(client).fetch_dated_snapshot(
                snapshot([snapshot_row('sh600519')]), DAY, checkpoint=checkpoints.append)
    assert checkpoints == []


def test_dated_snapshot_checkpoints_resume_without_repeating_successful_requests():
    rows = [snapshot_row(f'sh{600000 + index:06}') for index in range(41)]
    universe, saved, calls = snapshot(rows), [], []

    def interrupted(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(503, text='temporarily unavailable')
        return httpx.Response(200, text=''.join(hq_assignment(symbol)
                                              for symbol in request.url.params['list'].split(',')))

    with httpx.Client(transport=httpx.MockTransport(interrupted)) as client:
        with pytest.raises(SinaDataError) as caught:
            SinaMarketProvider(client).fetch_dated_snapshot(universe, DAY, checkpoint=saved.extend)
    assert caught.value.code == 'http_error'
    assert len(saved) == 40

    def resumed(request):
        assert request.url.params['list'] == 'sh600040'
        return httpx.Response(200, text=hq_assignment('sh600040'))

    with httpx.Client(transport=httpx.MockTransport(resumed)) as client:
        result = SinaMarketProvider(client).fetch_dated_snapshot(universe, DAY, cached_rows=saved)
    assert len(result['rows']) == 41
    assert len(result['raw']['hq_requests']) == 1
    assert result['dated_snapshot_verified'] is True


@pytest.mark.parametrize('problem', ['stale_raw', 'changed_raw_price', 'extra_symbol', 'duplicate_symbol'])
def test_dated_snapshot_revalidates_saved_raw_before_network(problem):
    universe = snapshot([snapshot_row('sh600519')])
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=hq_assignment('sh600519')))) as client:
        saved = SinaMarketProvider(client).fetch_dated_snapshot(universe, DAY)['rows']
    if problem == 'stale_raw':
        saved[0]['hq_raw'][30] = str(DAY - timedelta(days=1))
    elif problem == 'changed_raw_price':
        saved[0]['hq_raw'][3] = '10.01'
    elif problem == 'extra_symbol':
        saved[0]['symbol'] = 'sz000001'
    elif problem == 'duplicate_symbol':
        saved.append(deepcopy(saved[0]))

    def unexpected(request):
        pytest.fail('Invalid checkpoint must fail before another network request')

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(SinaDataError):
            SinaMarketProvider(client).fetch_dated_snapshot(universe, DAY, cached_rows=saved)


def test_dated_snapshot_cancellation_after_batch_keeps_verifiable_checkpoint():
    universe = snapshot([snapshot_row('sh600519')])
    saved = []

    def checkpoint(rows):
        saved.extend(rows)
        raise RuntimeError('worker cancelled')

    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=hq_assignment('sh600519')))) as client:
        with pytest.raises(RuntimeError, match='worker cancelled'):
            SinaMarketProvider(client).fetch_dated_snapshot(universe, DAY, checkpoint=checkpoint)
    assert saved[0]['date'] == str(DAY)
    assert saved[0]['hq_raw'][30] == str(DAY)
