"""Pure evidence rules for the market heat and crowding collectors."""

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.services.market_methodology import JOINQUANT_HEAT_METHOD, LEGACY_SINA_HEAT_METHOD

SAFE_ERRORS = {
    'source_unavailable',
    'source_not_ready',
    'source_rate_limited',
    'source_forbidden',
    'calendar_unavailable',
    'calendar_incomplete',
    'calendar_regressed',
    'snapshot_invalid',
    'historical_snapshot_missing',
    'quote_unavailable',
    'quote_mismatch',
    'unknown_missing_ma',
    'quote_stale',
    'source_partial',
    'service_interrupted',
    'cancelled',
    'missing_writer_configuration',
    'writer_permission_denied',
    'writer_table_missing',
    'writer_database_missing',
    'writer_connection_failed',
    'writer_write_failed',
    'writer_journal_incomplete',
    'writer_value_mismatch',
    'writer_result_invalid',
    'before_collection_time',
    'worker_disabled',
    'universe_coverage_unverified',
    'universe_incomplete',
    'adjustment_unavailable',
    'adjustment_precision_unverified',
    'paused_unknown',
    'invalid_tdx_archive',
    'historical_count_unavailable',
    'historical_count_mismatch',
    'historical_identity_conflict',
    'historical_category_unknown',
    'unsupported_historical_schema',
    'heat_methodology_unverified',
    'heat_methodology_mismatch',
    'historical_snapshot_unverified',
    'writer_methodology_mismatch',
    'snapshot_quote_mismatch',
    'dated_report_invalid',
    'dated_report_cache_invalid',
    'dated_membership_window_unverified',
    'dated_membership_gap',
}


class SourceFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in SAFE_ERRORS else 'source_unavailable'
        super().__init__(self.code)


def number(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('invalid numeric observation') from None
    if not result.is_finite():
        raise ValueError('invalid numeric observation')
    return result


def heat_contract(heat: object) -> bool:
    return (
        isinstance(heat, dict)
        and heat.get('methodology') == JOINQUANT_HEAT_METHOD
        and heat.get('price_basis') == 'qfq'
        and heat.get('denominator') == 'eligible'
    )


def quote_method(quote: object) -> object:
    return (
        quote.get('methodology', LEGACY_SINA_HEAT_METHOD)
        if isinstance(quote, dict)
        else LEGACY_SINA_HEAT_METHOD
    )


def verified_heat_quote(
    quote: object, *, method: Callable[[object], object] = quote_method
) -> bool:
    return (
        isinstance(quote, dict)
        and method(quote) == JOINQUANT_HEAT_METHOD
        and quote.get('verified') is True
    )


def heat_capability(result: object) -> dict[str, object]:
    verified = (
        isinstance(result, dict)
        and result.get('methodology') == JOINQUANT_HEAT_METHOD
        and result.get('verified') is True
    )
    return {
        'methodology': JOINQUANT_HEAT_METHOD,
        'verified': verified,
        'error': None if verified else 'heat_methodology_unverified',
    }


def validate_snapshot(snapshot: Any, day: str) -> None:
    try:
        rows = snapshot['rows']
        symbols = [row['symbol'] for row in rows]
        if (
            snapshot['date'] != day
            or not rows
            or snapshot['expected_count'] != len(rows)
            or len(set(symbols)) != len(rows)
        ):
            raise ValueError()
        for row in rows:
            if number(row['amount']) < 0 or number(row['volume']) < 0 or number(row['close']) < 0:
                raise ValueError()
        if sum((number(row['amount']) for row in rows), Decimal(0)) <= 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise SourceFailure('snapshot_invalid') from None


def dated_snapshot(
    snapshot: Any, day: str, *, validate: Callable[[Any, str], None] = validate_snapshot
) -> bool:
    try:
        validate(snapshot, day)
        return (
            snapshot.get('source_has_trading_date') is True
            and snapshot.get('dated_snapshot_verified') is True
            and snapshot.get('date_verified_by') == 'sina_hq'
            and all(row.get('date') == day for row in snapshot['rows'])
        )
    except (SourceFailure, AttributeError, TypeError):
        return False


def validate_hq_batch(day: str, node_rows: Mapping[str, Any], rows: Iterable[Any]) -> None:
    symbols = set()
    try:
        for row in rows:
            symbol = row['symbol']
            node = node_rows[symbol]
            if (
                symbol in symbols
                or row['date'] != day
                or number(row['close']) != number(node['close'])
                or number(row['volume']) != number(node['volume'])
                or number(row['amount']).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
                != number(node['amount'])
            ):
                raise ValueError()
            symbols.add(symbol)
    except (KeyError, TypeError, ValueError):
        raise SourceFailure('snapshot_invalid') from None


def fresh_quote(quote: Any, snapshot_row: Any, day: str) -> bool:
    try:
        return (
            quote.get('status') in ('current', 'insufficient_history', 'unknown_missing_ma')
            and quote['symbol'] == snapshot_row['symbol']
            and quote['day'] == quote['date'] == day
            and number(quote['close']) == number(snapshot_row['close']) > 0
            and number(quote['volume']) == number(snapshot_row['volume']) > 0
        )
    except (AttributeError, KeyError, ValueError, TypeError):
        return False


def price_verified(
    quote: Any,
    snapshot_row: Any,
    day: str,
    *,
    fresh: Callable[[Any, Any, str], bool] = fresh_quote,
    verified_heat: Callable[[object], bool] = verified_heat_quote,
) -> bool:
    if fresh(quote, snapshot_row, day):
        return True
    try:
        if (
            verified_heat(quote)
            and quote.get('status') == 'paused'
            and quote.get('paused') == 1
            and quote['symbol'] == snapshot_row['symbol']
            and quote['day'] == quote['date'] == day
            and number(quote['close']) == number(snapshot_row['close']) > 0
            and number(quote['volume'])
            == number(snapshot_row['volume'])
            == number(snapshot_row['amount'])
            == 0
        ):
            return True
        return (
            quote.get('status') == 'no_trade'
            and quote['symbol'] == snapshot_row['symbol']
            and quote['day'] == day
            and date.fromisoformat(quote['date']) < date.fromisoformat(day)
            and number(quote['close']) > 0
            and number(snapshot_row['volume']) == number(snapshot_row['amount']) == 0
        )
    except (AttributeError, KeyError, ValueError, TypeError):
        return False


def verification_samples(rows: Iterable[Any]) -> list[Any] | None:
    """One strongest traded quote per market, or no admissible sample set."""
    samples = []
    for market in ('sh', 'sz', 'bj'):
        candidates = [
            row
            for row in rows
            if row['symbol'].startswith(market)
            and number(row['amount']) > 0
            and number(row['volume']) > 0
            and number(row['close']) > 0
        ]
        if not candidates:
            return None
        samples.append(max(candidates, key=lambda row: (number(row['amount']), row['symbol'])))
    return samples
