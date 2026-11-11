"""Allowlisted source failure context, independent of public retry/error codes.

Never retain response bodies, URLs, request parameters or exception messages.
Revalidate fields when copying and rendering, even if an injected adapter set
the attributes. Nested contexts retain the innermost known failure stage.
"""
from contextlib import contextmanager
from datetime import date
import socket
import ssl

import httpx


_SOURCES = frozenset({
    'eastmoney_dated_universe', 'eastmoney_listing_metadata',
    'exchange_master', 'sse_current', 'sse_delisted', 'szse_current', 'szse_cdr',
    'szse_delisted', 'szse_listing_suspended',
    'tdx_daily_archive',
})
_STAGES = frozenset({
    'cache_read', 'cache_write', 'window_validation',
    'catalogue_validation', 'request', 'json_parse', 'page_validation',
    'metadata_validation', 'workbook_parse', 'rows_validation',
    'zip_parse', 'records_parse',
})
_CODES = frozenset({
    'dated_report_invalid', 'dated_report_cache_invalid', 'dated_membership_window_unverified',
    'dated_membership_gap', 'cache_corrupt', 'invalid_numeric',
    'ambiguous_listing_intervals', 'capture_date_changed', 'count_changed', 'duplicate_symbol',
    'empty_universe', 'future_universe', 'http_error', 'incomplete_page',
    'invalid_count', 'invalid_date', 'invalid_interval', 'invalid_json',
    'invalid_name', 'invalid_page', 'invalid_symbol', 'invalid_type',
    'invalid_universe', 'invalid_workbook', 'missing_market', 'network_error',
    'rate_limited', 'request_deadline', 'catalogue_deadline', 'response_too_large',
    'invalid_tdx_archive',
})
_TRANSPORT_TYPES = (
    (httpx.ConnectTimeout, 'connect_timeout'),
    (httpx.ReadTimeout, 'read_timeout'),
    (httpx.WriteTimeout, 'write_timeout'),
    (httpx.PoolTimeout, 'pool_timeout'),
    (httpx.ConnectError, 'connect_error'),
    (httpx.ReadError, 'read_error'),
    (httpx.WriteError, 'write_error'),
    (httpx.DecodingError, 'decoding_error'),
    (httpx.ProxyError, 'proxy_error'),
    (httpx.ProtocolError, 'protocol_error'),
)
_TRANSPORT_CATEGORIES = frozenset(category for _, category in _TRANSPORT_TYPES) | {'other_httpx'}
_UNDERLYING_KINDS = frozenset({'ssl_certificate_verification', 'dns_error'})
_MAX_CAUSES = 8
_CACHE_STATES = frozenset({'hit', 'miss', 'disabled'})
_PROGRESS_LIMITS = {'archive_bytes': 1024**4, 'elapsed_ms': 7 * 24 * 60 * 60 * 1000}


def _known(value, allowed):
    return value if type(value) is str and value in allowed else None


def _errno(value):
    return value if type(value) is int and -(2**31) <= value < 2**31 else None


def _source_date(value):
    if type(value) is not str or len(value) != 10:
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _safe_context(error):
    context = getattr(error, 'source_diagnostics', None)
    context = context if type(context) is dict else {}
    status = context.get('status_code')
    result = {
        'source_id': _known(context.get('source_id'), _SOURCES),
        'stage': _known(context.get('stage'), _STAGES),
        'code': _known(context.get('code'), _CODES),
        'status_code': status if type(status) is int and 100 <= status <= 599 else None,
    }
    transport = _known(context.get('transport_category'), _TRANSPORT_CATEGORIES)
    underlying = _known(context.get('underlying_kind'), _UNDERLYING_KINDS)
    if transport:
        result['transport_category'] = transport
        if underlying:
            result['underlying_kind'] = underlying
            number = _errno(context.get('underlying_errno'))
            if number is not None:
                result['underlying_errno'] = number
    source_date = _source_date(context.get('source_date'))
    if source_date:
        result['source_date'] = source_date
    cache_state = _known(context.get('cache_state'), _CACHE_STATES)
    if cache_state:
        result['cache_state'] = cache_state
    for key, limit in _PROGRESS_LIMITS.items():
        value = context.get(key)
        if type(value) is int and 0 <= value <= limit:
            result[key] = value
    number = _errno(context.get('os_errno'))
    if number is not None:
        result['os_errno'] = number
    return result


def record_transport_failure(error, wrapped):
    """Capture canonical transport facts before a safe public wrapper hides them.

    Inspect bounded cause/context links only, not messages, args, requests or
    arbitrary class names. SSL/DNS classification requires a real canonical
    exception type; a message claiming a TLS or DNS failure is not evidence.
    """
    if not isinstance(error, httpx.HTTPError):
        return wrapped
    context = _safe_context(wrapped)
    context['transport_category'] = next((category for kind, category in _TRANSPORT_TYPES
                                          if isinstance(error, kind)), 'other_httpx')
    current, seen = error, set()
    while isinstance(current, BaseException) and id(current) not in seen and len(seen) < _MAX_CAUSES:
        seen.add(id(current))
        underlying = ('ssl_certificate_verification' if isinstance(current, ssl.SSLCertVerificationError)
                      else 'dns_error' if isinstance(current, socket.gaierror) else None)
        if underlying:
            context['underlying_kind'] = underlying
            number = _errno(current.errno)
            if number is not None:
                context['underlying_errno'] = number
            break
        current = (current.__cause__ if current.__cause__ is not None else
                   current.__context__ if not current.__suppress_context__ else None)
    wrapped.source_diagnostics = context
    return wrapped


def record_http_failure(error, wrapped):
    """Preserve canonical HTTP/transport facts without changing error semantics."""
    if not isinstance(error, httpx.HTTPError):
        return wrapped
    context = _safe_context(wrapped)
    if isinstance(error, httpx.HTTPStatusError):
        response = getattr(error, 'response', None)
        status = response.status_code if isinstance(response, httpx.Response) else None
        if type(status) is int and 100 <= status <= 599:
            context['status_code'] = status
        context['code'] = context['code'] or 'http_error'
        wrapped.source_diagnostics = context
        return wrapped
    context['code'] = context['code'] or 'network_error'
    wrapped.source_diagnostics = context
    return record_transport_failure(error, wrapped)


def record_source_progress(error, *, source_date=None, cache_state=None,
                           archive_bytes=None, elapsed_ms=None):
    """Attach only bounded counters and canonical dates; never paths or headers."""
    if getattr(error, 'stop_collection', False):
        return error
    context = _safe_context(error)
    for key, value in (('source_date', source_date), ('cache_state', cache_state),
                       ('archive_bytes', archive_bytes), ('elapsed_ms', elapsed_ms)):
        if value is not None:
            context[key] = value
    error.source_diagnostics = context
    error.source_diagnostics = _safe_context(error)
    return error


def record_os_failure(error, wrapped):
    """Retain a real OS error's bounded errno, without its path or message."""
    if isinstance(error, OSError) and not getattr(error, 'stop_collection', False):
        context = _safe_context(wrapped)
        number = _errno(error.errno)
        if number is not None:
            context['os_errno'] = number
        wrapped.source_diagnostics = context
    return wrapped


@contextmanager
def source_failure_context(*, source_id=None, stage=None, status_code=None):
    try:
        yield
    except Exception as error:
        # Cancellation must propagate unchanged, never become a source failure.
        if not getattr(error, 'stop_collection', False):
            context = _safe_context(error)
            context['source_id'] = context['source_id'] or _known(source_id, _SOURCES)
            context['stage'] = context['stage'] or _known(stage, _STAGES)
            context['code'] = context['code'] or _known(getattr(error, 'code', None), _CODES)
            status = getattr(error, 'status_code', None)
            if context['status_code'] is None and type(status) is int and 100 <= status <= 599:
                context['status_code'] = status
            if context['status_code'] is None and type(status_code) is int and 100 <= status_code <= 599:
                context['status_code'] = status_code
            error.source_diagnostics = context
        raise


def copy_source_failure(error, wrapped):
    """Preserve safe source facts without changing public code/retry semantics."""
    wrapped.source_diagnostics = _safe_context(error)
    return wrapped


def source_failure_details(error):
    context = _safe_context(error)
    fields = [f'{key}={context[field] if context[field] is not None else "-"}'
              for key, field in (('source_id', 'source_id'), ('source_stage', 'stage'),
                                 ('source_code', 'code'), ('http_status', 'status_code'))]
    fields.extend(f'{key}={context[key]}' for key in (
        'transport_category', 'underlying_kind', 'underlying_errno',
        'source_date', 'cache_state', 'archive_bytes', 'elapsed_ms', 'os_errno')
                  if key in context)
    return ' '.join(fields)
