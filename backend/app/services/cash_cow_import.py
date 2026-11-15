"""Read the small JoinQuant result from the existing immutable local cache."""
import math

from sqlalchemy import select
from app.core.cache_errors import SnapshotDataError
from app.models.cash_cow import CashCowSnapshot
from app.services.cash_cow_rules import PERIODS, evaluate_year, finish_stock

RULE_VERSION = 'cash_cow_static_display_v2'


def validate_snapshot(connection):
    rows = connection.execute(select(CashCowSnapshot.__table__)).mappings().all()
    try:
        if len(rows) != 1 or rows[0]['id'] != 1:
            raise ValueError('Expected one complete snapshot')
        row = rows[0]
        payload = row['payload']
        if set(payload) != {str(p) for p in PERIODS}:
            raise ValueError('Missing screening window')
        run_ids = set()
        for period in PERIODS:
            result = payload[str(period)]
            meta, items = result['snapshot'], result['items']
            run_ids.add(meta['run_id'])
            if (meta['years'] != period or meta['as_of'] != row['as_of_date'].isoformat()
                    or meta['rule_version'] != RULE_VERSION or meta['source'] != '聚宽'
                    or meta['end_year'] != row['as_of_date'].year - 1
                    or meta['start_year'] != meta['end_year'] - period + 1):
                raise ValueError('Mismatched provenance')
            counts = [meta[k] for k in ('universe_count', 'excluded_count', 'insufficient_count', 'matched_count')]
            if (any(type(n) is not int or n < 0 for n in counts) or counts[0] <= 0
                    or counts[0] != sum(counts[1:]) or counts[-1] != len(items)
                    or meta['evaluated_count'] != counts[1] + counts[-1]):
                raise ValueError('Incomplete counts')
            codes = [item['code'] for item in items]
            if len(codes) != len(set(codes)):
                raise ValueError('Repeated stock')
            for item in items:
                years = item['years']
                if [r['year'] for r in years] != list(range(meta['start_year'], meta['end_year'] + 1)):
                    raise ValueError('Incomplete annual history')
                for annual in years:
                    outcome, _ = evaluate_year(annual['year'], annual, annual)
                    if outcome != 'eligible':
                        raise ValueError('Annual criteria failed')
                outcome, computed = finish_stock(item, years,
                    {r['year']: r['dividend_per_share'] for r in years},
                    {r['year']: r for r in years}, period)
                if (outcome != 'matched' or not math.isclose(computed['average_yield'], item['average_yield'], rel_tol=1e-9)):
                    raise ValueError('Yield criteria failed')
        if len(run_ids) != 1 or not next(iter(run_ids)):
            raise ValueError('Mixed publications')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise SnapshotDataError('奶牛股结果不完整或口径不一致，请重新运行聚宽采集脚本。') from exc


def latest_cached(cache, years):
    from app.core.cache import CacheUnavailable
    try:
        with cache.session('cash-cow') as session:
            row = session.get(CashCowSnapshot, 1)
            return row.payload[str(years)] if row else None
    except CacheUnavailable:
        return None
