"""Allowlisted source failure context, independent of public retry/error codes.

Never retain response bodies, URLs, request parameters or exception messages.
Revalidate fields when copying and rendering, even if an injected adapter set
the attributes. Nested contexts retain the innermost known failure stage.
"""
from contextlib import contextmanager


_SOURCES = frozenset({
    'exchange_master', 'sse_current', 'sse_delisted', 'szse_current', 'szse_cdr',
    'szse_delisted', 'szse_listing_suspended',
})
_STAGES = frozenset({
    'catalogue_validation', 'request', 'json_parse', 'page_validation',
    'metadata_validation', 'workbook_parse', 'rows_validation',
})
_CODES = frozenset({
    'ambiguous_listing_intervals', 'count_changed', 'duplicate_symbol',
    'empty_universe', 'future_universe', 'http_error', 'incomplete_page',
    'invalid_count', 'invalid_date', 'invalid_interval', 'invalid_json',
    'invalid_name', 'invalid_page', 'invalid_symbol', 'invalid_type',
    'invalid_universe', 'invalid_workbook', 'missing_market', 'network_error',
    'rate_limited',
})


def _known(value, allowed):
    return value if type(value) is str and value in allowed else None


def _safe_context(error):
    context = getattr(error, 'source_diagnostics', None)
    context = context if type(context) is dict else {}
    status = context.get('status_code')
    return {
        'source_id': _known(context.get('source_id'), _SOURCES),
        'stage': _known(context.get('stage'), _STAGES),
        'code': _known(context.get('code'), _CODES),
        'status_code': status if type(status) is int and 100 <= status <= 599 else None,
    }


@contextmanager
def source_failure_context(*, source_id=None, stage=None):
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
            error.source_diagnostics = context
        raise


def copy_source_failure(error, wrapped):
    """Preserve safe source facts without changing public code/retry semantics."""
    wrapped.source_diagnostics = _safe_context(error)
    return wrapped


def source_failure_details(error):
    context = _safe_context(error)
    return ' '.join(f'{key}={context[field] if context[field] is not None else "-"}'
                    for key, field in (('source_id', 'source_id'), ('source_stage', 'stage'),
                                       ('source_code', 'code'), ('http_status', 'status_code')))
