"""Date-specific historical reconstruction audit, not a count-only whitelist.

Each candidate needs affirmative target-day existence (a real trade, or a
proved full-day suspension within its exchange listing interval). Official
category counts then guard completeness. Conflicting extra dated equities or
suspensions block the date. This remains a multi-source reconstruction rather
than a directly retrieved JoinQuant historical security list.
"""
from collections import Counter
from datetime import timedelta
import re

from app.services.market_sina import SinaDataError, _day, _number, _symbol

CATEGORIES = ('sse_main_a', 'sse_star', 'szse_main_a', 'szse_chinext')
DATED_SOURCE = 'eastmoney_dated_universe'
DATED_EVIDENCE_VERSION = 1
DATED_MEMBERSHIP_BASIS = 'dated_report_trade_or_confirmed_pause_and_official_category_counts'


def equity_namespace(symbol):
    """Broad contradiction detection only, never select the candidate universe.

    Keep conventional SSE equity and SZ main/ChiNext namespaces. Unknown board
    families selected by the exchange catalogue are still retained and audited.
    """
    return isinstance(symbol, str) and (
        symbol.startswith('sh6') or symbol.startswith('sz00') or symbol.startswith('sz30'))


def category(member):
    symbol = _symbol(member['symbol'])
    if member.get('evidence', {}).get('source_id') == DATED_SOURCE:
        # Namespace classifies already-selected identities; never filters the
        # report. Vendor names/risk-warning/industry labels are not board proof.
        for prefixes, label in ((('sh60',), 'sse_main_a'), (('sh688', 'sh689'), 'sse_star'),
                                (('sz00',), 'szse_main_a'), (('sz30',), 'szse_chinext')):
            if symbol.startswith(prefixes):
                return label
        raise SinaDataError('证券代码所属板块无法核实。', code='historical_category_unknown')
    if symbol.startswith('sh'):
        board = str(member.get('board', ''))
        if board in ('1', '8'):
            return 'sse_main_a' if board == '1' else 'sse_star'
    else:
        board = member.get('board')
        if board in ('主板', '主板A股', '中小企业板', '中小板'):
            return 'szse_main_a'
        if board in ('创业板', '创业板A股'):
            return 'szse_chinext'
        # Historical terminated SZ catalogues do not carry board metadata.
        # Code families classify already-selected official records only.
        if symbol.startswith('sz00'):
            return 'szse_main_a'
        if symbol.startswith('sz30'):
            return 'szse_chinext'
    raise SinaDataError('历史证券板块分类无法核实。', code='historical_category_unknown')


def affirmative_pause(interval, day):
    if not isinstance(interval, dict):
        return False
    proved = interval.get('full_day') is True or (
        interval.get('paused') == 1 and interval.get('evidence', {}).get('full_day_verified') is True)
    try:
        return proved and _day(interval['start']) <= day and (
            interval.get('end') is None or day <= _day(interval['end']))
    except (SinaDataError, KeyError):
        return False


def audit_historical_membership(master, snapshot_rows, raw_equities, suspensions, official):
    day = _day(master['date'])
    evidence = master.get('evidence', {})
    dated = evidence.get('source_id') == DATED_SOURCE
    if dated:
        if (type(evidence.get('evidence_version')) is not int or evidence['evidence_version'] != DATED_EVIDENCE_VERSION
                or evidence.get('target_date') != str(day)
                or any(evidence.get(key) is not True for key in
                       ('source_counts_verified', 'report_date_verified', 'report_pages_complete'))
                or not re.fullmatch(r'[0-9a-f]{64}', str(evidence.get('report_digest', '')))):
            raise SinaDataError('逐日证券报告缺少完整日期和分页证据。', code='universe_coverage_unverified')
    elif (any(evidence.get(key) is not True for key in
            ('source_counts_verified', 'listing_dates_verified', 'current_catalogue_verified'))
            or _day(evidence['snapshot_date']) < day):
        raise SinaDataError('历史重建缺少完整交易所证券区间。', code='universe_coverage_unverified')
    if (official.get('date') != str(day) or official.get('verified') is not True
            or set(official.get('counts', {})) != set(CATEGORIES)
            or any(type(value) is not int or value < 0 for value in official['counts'].values())):
        raise SinaDataError('官方目标日分类数量不完整。', code='historical_count_unavailable')
    archive_evidence = master.get('source_archive_evidence', {})
    if (archive_evidence.get('date') != str(day) or not isinstance(archive_evidence.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', archive_evidence['sha256'])):
        raise SinaDataError('历史行情包审计摘要缺失。', code='historical_identity_conflict')
    members = master['rows']
    by_symbol = {row['symbol']: row for row in snapshot_rows}
    symbols = {member['symbol'] for member in members}
    if (not members or len(symbols) != len(members) or len(by_symbol) != len(snapshot_rows)
            or symbols != set(by_symbol) or len(members) != master.get('expected_count')):
        raise SinaDataError('历史候选池与逐股证据不一致。', code='universe_incomplete')
    categories = Counter()
    traded = paused = 0
    for member in members:
        symbol = _symbol(member['symbol'])
        row = by_symbol[symbol]
        if dated:
            source = member.get('evidence', {})
            if (member.get('date') != str(day) or source.get('source_id') != DATED_SOURCE
                    or source.get('report_date') != str(day) or row.get('date') != str(day)):
                raise SinaDataError('逐日证券报告与目标行情身份冲突。', code='historical_identity_conflict')
        else:
            start = _day(member['listed_from'])
            end = _day(member['listed_until']) if member.get('listed_until') else None
            if end is not None and member.get('listed_until_exclusive') is True:
                end -= timedelta(days=1)
            if (not start <= day or (end is not None and day > end) or row.get('date') != str(day)):
                raise SinaDataError('证券上市区间或目标行情日期冲突。', code='historical_identity_conflict')
        volume, amount = _number(row['volume']), _number(row['amount'])
        _number(row['close'], positive=True)
        if row.get('paused') == 0 and volume > 0 and amount > 0:
            raw = raw_equities.get(symbol)
            if (not isinstance(raw, dict) or raw.get('date') != str(day)
                    or _number(raw['volume']) != volume or _number(raw['amount']) != amount
                    or _number(raw['close']) != _number(row['close'])):
                raise SinaDataError('历史证券缺少目标日真实成交证据。', code='historical_identity_conflict')
            traded += 1
        elif (row.get('paused') == 1 and volume == amount == 0
              and affirmative_pause(row.get('pause_evidence'), day)):
            raw = raw_equities.get(symbol)
            if raw is not None and (_number(raw['volume']) > 0 or _number(raw['amount']) > 0):
                raise SinaDataError('历史停牌与实际成交冲突。', code='historical_identity_conflict')
            paused += 1
        else:
            raise SinaDataError('历史候选证券缺少逐股存在证据。', code='historical_identity_conflict')
        categories[category(member)] += 1
    # A matching total cannot hide an excluded former code, relisting interval,
    # or a same-size replacement. Contradictory observed identities block first.
    extra_traded = [symbol for symbol, row in raw_equities.items() if symbol not in symbols
                    and equity_namespace(symbol) and (_number(row['volume']) > 0 or _number(row['amount']) > 0)]
    extra_paused = [symbol for symbol, intervals in suspensions.get('intervals', {}).items()
                    if symbol not in symbols and equity_namespace(symbol)
                    and any(affirmative_pause(interval, day) for interval in intervals)]
    if extra_traded or extra_paused:
        raise SinaDataError('目标日存在候选池之外的成交或停牌证券。', code='historical_identity_conflict')
    actual = {key: categories.get(key, 0) for key in CATEGORIES}
    if actual != official['counts']:
        raise SinaDataError('历史候选池与官方逐板块证券数量不符。', code='historical_count_mismatch')
    return {**evidence, 'membership_verified': True, 'universe_verified': True,
            'historical_membership_verified': False, 'historical_reconstruction_verified': True,
            'membership_basis': DATED_MEMBERSHIP_BASIS if dated else
                'dated_trade_or_confirmed_pause_plus_exchange_intervals_and_category_counts',
            'target_date': str(day), 'traded_identity_count': traded, 'paused_identity_count': paused,
            'official_counts': actual, 'count_evidence': official.get('evidence', {}),
            'category_basis': ({'sse': 'selected_report_identity_namespace_60_main_688_689_star',
                                'szse': 'selected_report_identity_namespace_00_main_30_chinext'} if dated else
                               {'sse': 'exchange_stock_type',
                                'szse': 'derived_from_security_code_category_for_catalogue_members'}),
            'source_archive_evidence': archive_evidence, 'suspension_scan_evidence': suspensions,
            'joinquant_universe_directly_verified': False,
            'limitations': ['逐日多源交叉核验的历史重建，不是聚宽历史证券清单的直接证明。']}
