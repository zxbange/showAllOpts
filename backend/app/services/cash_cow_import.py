"""Validate, persist and restore the authoritative external cash-cow snapshot."""
import logging
from time import monotonic

from app.core.diagnostics import diagnostic_details
from app.repositories import cash_cow_snapshot
from app.repositories.cash_cow_validation import PUBLIC_SOURCE as PUBLIC_SOURCE
from app.repositories.cash_cow_validation import RULE_VERSION as RULE_VERSION
from app.repositories.cash_cow_validation import validate_payload
from app.repositories.cash_cow_validation import validate_snapshot as validate_snapshot

LOG = logging.getLogger(__name__)

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
