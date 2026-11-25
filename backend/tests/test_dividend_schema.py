"""Schema-shape planning and MySQL DDL restart invariants without a live database."""

import pytest
from sqlalchemy import Integer, Numeric, String

from app.repositories import dividend_schema as m


def test_named_revisions_are_planned_from_actual_legacy_shape():
    legacy = {
        'dividend_cash_1y': m.ColumnShape(width=None, nullable=False),
    }
    assert [r.revision_id for r in m.plan_stock_columns(legacy, mysql=True)] == [
        'dividend-002-per-share',
        'dividend-003-fiscal-year',
        'dividend-004-cash-nullability',
    ]
    head = {'rule_version': m.ColumnShape(width=32, nullable=False)}
    assert m.plan_rule_capacity(head) == (m.RULE_CAPACITY,)
    head['rule_version'] = m.ColumnShape(width=128, nullable=False)
    assert m.plan_rule_capacity(head) == ()  # Wider custom column remains untouched.
    with pytest.raises(m.SchemaMigrationError, match='migration required'):
        m.plan_stock_columns(legacy, mysql=False)


def test_partial_mysql_ddl_resume_replans_and_never_replays_completed_step():
    columns = {'dividend_cash_1y': dict(type=Numeric(28, 6), nullable=False)}
    executed = []
    failed = [False]

    def read(table):
        assert table == m.STOCK
        return [dict(name=name, **metadata) for name, metadata in columns.items()]

    def execute(statement):
        executed.append(statement)
        if statement == m.STOCK_REVISIONS[0].statement:
            columns['dividend_per_share'] = dict(type=Numeric(24, 8), nullable=True)
        elif statement == m.STOCK_REVISIONS[1].statement:
            if not failed[0]:
                failed[0] = True
                raise RuntimeError('DDL interrupted')
            columns['dividend_fiscal_year'] = dict(type=Integer(), nullable=True)
        else:
            columns['dividend_cash_1y']['nullable'] = True

    def plan():
        return m.plan_stock_columns(m.column_shapes(read(m.STOCK)), mysql=True)

    with pytest.raises(RuntimeError, match='DDL interrupted'):
        m.apply_revisions(plan(), read_columns=read, execute=execute)
    assert plan() == m.STOCK_REVISIONS[1:]
    assert m.apply_revisions(plan(), read_columns=read, execute=execute) == (
        'dividend-003-fiscal-year',
        'dividend-004-cash-nullability',
    )
    assert executed.count(m.STOCK_REVISIONS[0].statement) == 1
    assert plan() == ()
    assert m.plan_stock_columns(m.column_shapes(read(m.STOCK)), mysql=False) == ()


def test_schema_plan_rechecks_completed_revision_before_writing():
    calls = []

    def read(_table):
        return [dict(name='rule_version', type=String(64), nullable=False)]

    assert not m.apply_revisions((m.RULE_CAPACITY,), read_columns=read, execute=calls.append)
    assert calls == []


def test_returned_ddl_without_matching_column_is_not_success():
    def read(_table):
        return [dict(name='rule_version', type=String(32), nullable=False)]

    with pytest.raises(m.SchemaMigrationError, match='dividend-001-rule-version-capacity'):
        m.apply_revisions((m.RULE_CAPACITY,), read_columns=read, execute=lambda _: None)


def test_schema_drift_cannot_convert_unknown_rule_column():
    def read(_table):
        return [dict(name='rule_version', type=Numeric(12, 2), nullable=False)]

    calls = []
    with pytest.raises(m.SchemaMigrationError, match='not applicable'):
        m.apply_revisions((m.RULE_CAPACITY,), read_columns=read, execute=calls.append)
    assert not calls
