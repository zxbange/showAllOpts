"""Versioned schema evidence for the existing dividend publication tables.

MySQL 5.7 and 8.4 DDL implicitly commits: never execute these steps within the
business snapshot transaction. Actual column postconditions, not an auxiliary
version table, certify each named revision. A retry plans from a fresh schema,
so an interrupted partial DDL sequence can resume without replaying completed
steps. This pilot does not add any migration not already used by the collector.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

ColumnRead = Callable[[str], Sequence[Mapping[str, object]]]
DDL = Callable[[str], None]

HEAD = 'dividend_selection_snapshot'
STOCK = 'dividend_selection_stock'


@dataclass(frozen=True)
class ColumnShape:
    width: int | None
    nullable: bool | None


Columns = Mapping[str, ColumnShape]


class SchemaMigrationError(ValueError):
    """An existing schema could not be verified; publication must stop."""


@dataclass(frozen=True)
class Revision:
    revision_id: str
    table: str
    statement: str
    satisfied: Callable[[Columns], bool]
    applicable: Callable[[Columns], bool] = lambda _: True


def column_shapes(raw: Sequence[Mapping[str, object]]) -> dict[str, ColumnShape]:
    result = {}
    for column in raw:
        name = column['name']
        if not isinstance(name, str) or name in result:
            raise SchemaMigrationError('Invalid dividend schema column metadata')
        width = getattr(column.get('type'), 'length', None)
        nullable = column.get('nullable')
        result[name] = ColumnShape(
            width=width if type(width) is int else None,
            nullable=nullable if type(nullable) is bool else None,
        )
    return result


def _rule_capacity(columns: Columns) -> bool:
    column = columns.get('rule_version')
    return column is not None and column.width is not None and column.width >= 64


def _known_rule_width(columns: Columns) -> bool:
    column = columns.get('rule_version')
    return column is not None and column.width is not None


RULE_CAPACITY = Revision(
    'dividend-001-rule-version-capacity',
    HEAD,
    'ALTER TABLE dividend_selection_snapshot MODIFY COLUMN rule_version VARCHAR(64) NOT NULL',
    _rule_capacity,
    _known_rule_width,
)
STOCK_REVISIONS = (
    Revision(
        'dividend-002-per-share',
        STOCK,
        'ALTER TABLE dividend_selection_stock ADD COLUMN dividend_per_share NUMERIC(24,8) NULL',
        lambda columns: 'dividend_per_share' in columns,
    ),
    Revision(
        'dividend-003-fiscal-year',
        STOCK,
        'ALTER TABLE dividend_selection_stock ADD COLUMN dividend_fiscal_year INTEGER NULL',
        lambda columns: 'dividend_fiscal_year' in columns,
    ),
    Revision(
        'dividend-004-cash-nullability',
        STOCK,
        'ALTER TABLE dividend_selection_stock MODIFY COLUMN dividend_cash_1y NUMERIC(28,6) NULL',
        lambda columns: (
            'dividend_cash_1y' in columns and columns['dividend_cash_1y'].nullable is True
        ),
    ),
)


def plan_rule_capacity(columns: Columns) -> tuple[Revision, ...]:
    if 'rule_version' not in columns:
        raise SchemaMigrationError('Missing dividend rule_version')
    # Preserve the original collector's compatibility for unknown-width
    # column types, but never call such a type a verified VARCHAR upgrade.
    return (
        ()
        if not _known_rule_width(columns) or RULE_CAPACITY.satisfied(columns)
        else (RULE_CAPACITY,)
    )


def plan_stock_columns(columns: Columns, *, mysql: bool) -> tuple[Revision, ...]:
    if not mysql:
        if not {'dividend_per_share', 'dividend_fiscal_year'} <= columns.keys():
            raise SchemaMigrationError('Dividend database schema migration required')
        # Never auto-alter SQLite caches; existing code recreates snapshots.
        return ()
    cash = columns.get('dividend_cash_1y')
    if cash is None or cash.nullable is None:
        raise SchemaMigrationError('Missing or unverified dividend_cash_1y metadata')
    return tuple(revision for revision in STOCK_REVISIONS if not revision.satisfied(columns))


def apply_revisions(
    revisions: Sequence[Revision],
    *,
    read_columns: ColumnRead,
    execute: DDL,
) -> tuple[str, ...]:
    """Perform only outstanding legacy DDL and verify each postcondition.

    If another writer has already migrated the column, fresh inspection avoids
    replaying it. A concurrent alteration may still race; propagate that error
    rather than claiming migration success. The durable publication retry can
    replan from the actual schema. No exception is swallowed or treated as a
    successful DDL/rollback.
    """
    applied = []
    for revision in revisions:
        current = column_shapes(read_columns(revision.table))
        if revision.satisfied(current):
            continue
        if not revision.applicable(current):
            raise SchemaMigrationError(
                f'Dividend schema revision not applicable: {revision.revision_id}'
            )
        execute(revision.statement)
        if not revision.satisfied(column_shapes(read_columns(revision.table))):
            raise SchemaMigrationError(
                f'Dividend schema revision not verified: {revision.revision_id}'
            )
        applied.append(revision.revision_id)
    return tuple(applied)
