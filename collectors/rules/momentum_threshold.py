"""Exact industry-momentum threshold, embedded into both JoinQuant entry points."""

# BEGIN SHARED MOMENTUM THRESHOLD
from decimal import Decimal as _MomentumDecimal, InvalidOperation as _MomentumInvalidOperation


def _momentum_front_status(start_close, end_close):
    """Return strict >20% membership, or None for an invalid price pair.

    Use the decimal values supplied by the data source, without float arithmetic
    or rounding. Integer cross-products implement 5 * end > 6 * start exactly,
    including decimal inputs longer than the active Decimal context precision.
    """
    try:
        start = _MomentumDecimal(str(start_close))
        end = _MomentumDecimal(str(end_close))
    except (_MomentumInvalidOperation, TypeError, ValueError):
        return None
    if not start.is_finite() or not end.is_finite() or start <= 0 or end <= 0:
        return None
    start_numerator, start_denominator = start.as_integer_ratio()
    end_numerator, end_denominator = end.as_integer_ratio()
    return (5 * end_numerator * start_denominator
            > 6 * start_numerator * end_denominator)
# END SHARED MOMENTUM THRESHOLD
