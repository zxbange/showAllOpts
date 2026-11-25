"""Validate, persist and restore the authoritative external cash-cow snapshot."""
import logging
import math
from time import monotonic

from app.core.cache_errors import SnapshotDataError
from app.core.diagnostics import diagnostic_details
from app.repositories import cash_cow_snapshot
from app.services.cash_cow_rules import PERIODS, evaluate_year, finish_stock

RULE_VERSION = 'cash_cow_payout100_v3'
PUBLIC_SOURCE = 'AShareHub财务与股票池 / 东方财富分红与股本 / Tushare不复权行情'
LOG = logging.getLogger(__name__)


def validate_snapshot(connection):
    rows = cash_cow_snapshot.read_all(connection)
    if len(rows) != 1 or rows[0]['id'] != 1:
        raise SnapshotDataError('奶牛股缺少完整结果，请在页面重新采集。')
    validate_payload(rows[0])


def validate_payload(row):
    try:
        payload = row['payload']
        if set(payload) != {str(p) for p in PERIODS}:
            raise ValueError('Missing screening window')
        run_ids = set()
        for period in PERIODS:
            result = payload[str(period)]
            meta, items = result['snapshot'], result['items']
            run_ids.add(meta['run_id'])
            if (meta['years'] != period or meta['as_of'] != row['as_of_date'].isoformat()
                    or meta['rule_version'] != RULE_VERSION or meta['source'] != PUBLIC_SOURCE
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
        raise SnapshotDataError('奶牛股结果不完整或口径不一致，请在页面重新采集外部数据。') from exc


def latest_cached(cache, years):
    from app.core.cache import CacheUnavailable
    try:
        with cache.session('cash-cow') as session:
            result = cash_cow_snapshot.read_period(session, years)
            return result if result and result.get('snapshot', {}).get('source') == PUBLIC_SOURCE else None
    except CacheUnavailable:
        return None


def write_public(engine, payload, check, *, progress=None):
    record = cash_cow_snapshot.candidate_record(payload)
    validate_payload(record)
    stage, started = None, monotonic()

    def phase(value):
        nonlocal stage, started
        if stage is not None:
            LOG.info('Cash cow database phase complete stage=%s seconds=%.3f', stage, monotonic() - started)
        stage, started = value, monotonic()
        if progress is not None:
            progress(value)
        LOG.info('Cash cow database phase started stage=%s', value)

    try:
        check()
        # MySQL DDL can implicitly commit; keep it outside the snapshot transaction.
        phase('schema')
        cash_cow_snapshot.ensure_schema(engine)
        phase('lock')
        result = cash_cow_snapshot.publish(
            engine, record, check=check, phase=phase,
            source=PUBLIC_SOURCE, validate_existing=validate_payload,
        )
        if not result.written:
            return result.payload
    except BaseException as exc:
        LOG.error('Cash cow database phase failed stage=%s seconds=%.3f details=%s',
                  stage, monotonic() - started, diagnostic_details(exc))
        raise
    else:
        LOG.info('Cash cow database phase complete stage=%s seconds=%.3f', stage, monotonic() - started)
    return payload
