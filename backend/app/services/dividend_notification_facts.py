"""Compact notification facts from verified dividend publications."""
from decimal import Decimal
import logging

from sqlalchemy import select

from app.models.dividend import DividendSnapshot, DividendStock
from app.services.dividend_limits import DIVIDEND_GROUP_LIMITS

LOG = logging.getLogger(__name__)
_META_FIELDS = ('trade_date', 'rule_version', 'universe_count', 'eligible_count',
                'value_candidates', 'value_count')


def dividend_facts(meta, records):
    """Summarize a snapshot whose full publication has already been verified."""
    facts = {key: meta[key] for key in ('universe_count', 'eligible_count', 'value_candidates')}
    groups = {}
    for group in ('value', 'bank'):
        rows = [row for row in records if row['method'] == group]
        best = max(rows, key=lambda row: Decimal(str(row['dividend_yield_1y'])), default=None)
        items = ([{'dividend_yield_1y': str(best['dividend_yield_1y']),
                   'pe_ttm': str(best['pe_ttm'])}] if best else [])
        groups[group] = {'count': len(rows), 'items': items}
    return facts | {'trade_date': meta['trade_date'].isoformat(), 'groups': groups}


def published_dividend_facts(cache):
    """Read only the bounded page publication; never refresh or inspect candidates."""
    if cache is None:
        return None
    try:
        # SnapshotCache.session opens the immutable local publication and checks
        # its database identity. It does not query the source database.
        with cache.session('dividend-selection') as session:
            head = session.execute(select(DividendSnapshot.__table__).where(
                DividendSnapshot.id == 1).limit(1)).mappings().first()
            if head is None:
                return None
            limit = sum(DIVIDEND_GROUP_LIMITS.values())
            records = [dict(row) for row in session.execute(
                select(DividendStock.__table__).limit(limit + 1)).mappings()]
            if len(records) > limit:
                raise ValueError('Dividend publication exceeds group capacity')
            meta = {field: head[field] for field in _META_FIELDS}
            # Local import avoids a module initialization cycle: the collector
            # also delegates its compact facts to this module.
            from app.services.dividend_collection import validate_result
            validate_result(meta, records)
            return dividend_facts(meta, records)
    except Exception as exc:
        LOG.info('Dividend published notification facts unavailable error_type=%s', type(exc).__name__)
        return None
