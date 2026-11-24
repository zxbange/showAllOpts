"""Notifications recover verified page facts without fetching or publishing data."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models.dividend import DividendSnapshot, DividendStock
from app.services.dividend_notification_facts import dividend_facts, published_dividend_facts
from test_dividend_collection import result


@pytest.fixture
def local_publication():
    engine = create_engine('sqlite://')
    DividendSnapshot.__table__.create(engine)
    DividendStock.__table__.create(engine)

    def unexpected(*args, **kwargs):
        raise AssertionError('Notification must not refresh, read collector state, or fetch source data')

    @contextmanager
    def session(module):
        assert module == 'dividend-selection'
        with Session(engine) as connection:
            yield connection

    cache = SimpleNamespace(session=session, refresh=unexpected, read_state=unexpected, source=unexpected)
    yield engine, cache
    engine.dispose()


def publish(engine, meta, records):
    with engine.begin() as connection:
        connection.execute(DividendSnapshot.__table__.insert().values(
            id=1, updated_at=datetime(2026, 11, 16, 23), **meta))
        if records:
            connection.execute(DividendStock.__table__.insert(), records)


def test_compact_facts_keep_both_groups_and_highest_static_yield():
    meta = dict(trade_date=date(2026, 11, 16), universe_count=5500,
                eligible_count=200, value_candidates=3)
    rows = [dict(method='bank', dividend_yield_1y=Decimal('5.40'), pe_ttm=Decimal('8.0')),
            dict(method='value', dividend_yield_1y=Decimal('5.50'), pe_ttm=Decimal('7.1')),
            dict(method='bank', dividend_yield_1y=Decimal('6.70'), pe_ttm=Decimal('7.0'))]
    assert dividend_facts(meta, rows) == dict(
        trade_date='2026-11-16', universe_count=5500, eligible_count=200, value_candidates=3,
        groups={'value': {'count': 1, 'items': [{'dividend_yield_1y': '5.50', 'pe_ttm': '7.1'}]},
                'bank': {'count': 2, 'items': [{'dividend_yield_1y': '6.70', 'pe_ttm': '7.0'}]}})


@pytest.mark.parametrize('legacy', [False, True])
def test_existing_page_publication_needs_no_notification_state(local_publication, legacy):
    engine, cache = local_publication
    meta, records = result()
    if legacy:
        meta['rule_version'] = 'dividend_dps_proposal_age3_v9'
    publish(engine, meta, records)
    statements = []
    event.listen(engine, 'before_cursor_execute',
                 lambda conn, cursor, statement, parameters, context, many:
                 statements.append((statement, parameters)))

    facts = published_dividend_facts(cache)

    assert facts['trade_date'] == '2026-11-16'
    assert facts['universe_count'] == 5500
    assert {key: group['count'] for key, group in facts['groups'].items()} == {'value': 1, 'bank': 1}
    assert all(Decimal(group['items'][0]['dividend_yield_1y']) == 5 for group in facts['groups'].values())
    assert len(statements) == 2
    assert all(statement.lstrip().startswith('SELECT') for statement, _ in statements)
    assert statements[0][1][-2:] == (1, 0)
    assert statements[1][1][-2:] == (71, 0)


def test_valid_empty_selection_has_published_date_and_zero_counts(local_publication):
    engine, cache = local_publication
    meta, _ = result()
    meta.update(value_count=0, value_candidates=0)
    publish(engine, meta, [])
    facts = published_dividend_facts(cache)
    assert facts['trade_date'] == '2026-11-16'
    assert facts['groups'] == {'value': {'count': 0, 'items': []}, 'bank': {'count': 0, 'items': []}}


@pytest.mark.parametrize('corruption', ['missing_group', 'different_date', 'wrong_yield', 'wrong_count'])
def test_incomplete_or_invalid_publication_is_not_reported(local_publication, corruption, caplog):
    engine, cache = local_publication
    meta, records = result()
    records[0]['name'] = 'PRIVATE-STOCK-PAYLOAD'
    if corruption == 'missing_group':
        records = records[:1]
    elif corruption == 'different_date':
        records[0]['trade_date'] = date(2026, 11, 13)
    elif corruption == 'wrong_yield':
        records[0]['dividend_yield_1y'] = Decimal('9')
    else:
        meta['value_count'] += 1
    publish(engine, meta, records)
    caplog.set_level('INFO')
    assert published_dividend_facts(cache) is None
    assert 'error_type=DividendCollectionError' in caplog.text
    assert 'PRIVATE-STOCK-PAYLOAD' not in caplog.text


def test_over_capacity_publication_is_rejected_without_unbounded_read(local_publication):
    engine, cache = local_publication
    meta, original = result()
    records = []
    for index in range(72):
        row = deepcopy(original[0])
        row.update(method='value', code=f'{index:06d}.SH', rank=index + 1)
        records.append(row)
    meta.update(eligible_count=72, value_candidates=72, value_count=72)
    publish(engine, meta, records)
    statements = []
    event.listen(engine, 'before_cursor_execute',
                 lambda conn, cursor, statement, parameters, context, many:
                 statements.append((statement, parameters)))
    assert published_dividend_facts(cache) is None
    assert len(statements) == 2
    assert statements[1][1][-2:] == (71, 0)


def test_missing_legacy_stock_columns_do_not_trigger_refresh(local_publication, caplog):
    engine, cache = local_publication
    meta, _ = result()
    publish(engine, meta, [])
    with engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE dividend_selection_stock')
        connection.exec_driver_sql('CREATE TABLE dividend_selection_stock (code TEXT)')
    caplog.set_level('INFO')
    assert published_dividend_facts(cache) is None
    assert 'error_type=OperationalError' in caplog.text
    assert 'SELECT' not in caplog.text


def test_missing_metadata_or_unavailable_cache_has_no_published_facts(local_publication, caplog):
    _, cache = local_publication
    assert published_dividend_facts(cache) is None
    assert published_dividend_facts(None) is None

    @contextmanager
    def refused(module):
        raise RuntimeError('PRIVATE-DATABASE-IDENTITY')
        yield  # pragma: no cover

    caplog.set_level('INFO')
    assert published_dividend_facts(SimpleNamespace(session=refused)) is None
    assert 'error_type=RuntimeError' in caplog.text
    assert 'PRIVATE-DATABASE-IDENTITY' not in caplog.text
