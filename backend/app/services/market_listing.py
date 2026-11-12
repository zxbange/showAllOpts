"""Explicit listing evidence routing; catalogue absence is never IPO evidence."""
import logging
import time
import httpx

LOG = logging.getLogger(__name__)


def transient_connection_failure(error):
    if getattr(error, 'stop_collection', False) or getattr(error, 'status_code', None) in (401, 403, 429):
        return False
    if isinstance(error, (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError)):
        return True
    diagnostic = getattr(error, 'source_diagnostics', {})
    return (isinstance(diagnostic, dict) and diagnostic.get('transport_category') in
            ('connect_timeout', 'connect_error', 'read_timeout', 'read_error'))


def validate_listing_evidence(proof, symbol, day):
    from app.services.market_eastmoney_listing import validate_listing_evidence as eastmoney
    if isinstance(proof, dict) and proof.get('source_id') == 'asharehub_listing_metadata':
        from app.services.market_asharehub_listing import validate_listing_evidence as asharehub
        return asharehub(proof, symbol, day)
    return eastmoney(proof, symbol, day)


class ListingMetadataSources:
    """Prefer the batch directory, fall back only for absence or transient I/O.

    Denials, invalid identities, conflicting dates and corrupt evidence never
    grant permission to try an alternative source.
    """
    def __init__(self, primary, fallback, *, monotonic=None):
        self.primary, self.fallback = primary, fallback
        self._monotonic = monotonic or time.monotonic
        self._primary_retry_after = 0.0

    def fetch(self, symbol, day):
        if self._monotonic() < self._primary_retry_after:
            LOG.info('Market listing source cooldown symbol=%s date=%s source_id=asharehub_listing_metadata target=eastmoney_listing_metadata',
                     symbol, day)
            return self.fallback.fetch(symbol, day)
        try:
            return self.primary.fetch(symbol, day)
        except Exception as error:
            if getattr(error, 'stop_collection', False) or getattr(error, 'status_code', None) in (401, 403, 429):
                raise
            if not (transient_connection_failure(error) or
                    getattr(error, 'code', None) == 'listing_not_found'):
                raise
            if transient_connection_failure(error):
                self._primary_retry_after = self._monotonic() + 60.0
            LOG.warning('Market listing fallback symbol=%s date=%s source_id=asharehub_listing_metadata target=eastmoney_listing_metadata type=%s',
                        symbol, day, type(error).__name__)
            return self.fallback.fetch(symbol, day)
