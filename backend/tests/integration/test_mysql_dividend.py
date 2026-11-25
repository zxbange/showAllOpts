"""Run against an empty disposable MySQL service, never the configured application DB.

Opt in with SHOWALLOPTS_TEST_MYSQL=1 and SHOWALLOPTS_TEST_MYSQL_PORT. The
fixed credentials below identify the synthetic CI container only. The test
does not read a .env, accept a DSN, contact non-local hosts, or use production
credentials. A configured but unreachable/incorrect/nonempty service is a
test failure, not a skip.
"""

import json
import math
import os
from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal

import pytest
from pymysql.err import OperationalError
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import URL
from test_cash_cow_snapshot_repository import exercise_publication_contract

from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.models.cash_cow import CashCowSnapshot
from app.models.dividend import DividendSnapshot, DividendStock
from app.services import collection_notifications, dividend_collection
from app.services.dividend_collection import (
    CHINA,
    DividendCollection,
    DividendCollectionError,
    records_match,
    write_result,
)
from app.services.dividend_public_source import rank_records

HOST = '127.0.0.1'
DATABASE = USER = 'showallopts_ci'
PASSWORD = 'showallopts_ci_password'
DAY = date(2026, 11, 16)
MOMENT = datetime(2026, 11, 16, 23, tzinfo=CHINA)
HEAD, STOCK = DividendSnapshot.__table__, DividendStock.__table__


@pytest.fixture
def mysql_engine():
    if os.environ.get('SHOWALLOPTS_TEST_MYSQL') != '1':
        pytest.skip('Explicit disposable MySQL integration service required')
    raw_port = os.environ.get('SHOWALLOPTS_TEST_MYSQL_PORT')
    if not raw_port or not raw_port.isascii() or not raw_port.isdigit():
        pytest.fail('Explicit disposable MySQL port required')
    port = int(raw_port)
    if not 1 <= port <= 65535:
        pytest.fail('Invalid disposable MySQL port')
    url = URL.create(
        'mysql+pymysql',
        username=USER,
        password=PASSWORD,
        host=HOST,
        port=port,
        database=DATABASE,
        query={'charset': 'utf8mb4'},
    )
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        connect_args={'connect_timeout': 5, 'read_timeout': 10, 'write_timeout': 10},
    )
    try:
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT DATABASE()')) == DATABASE
            assert connection.scalar(text('SELECT CURRENT_USER()')).split('@')[0] == USER
            version = connection.scalar(text('SELECT VERSION()'))
            assert 'MariaDB' not in version and version.startswith(('5.7.', '8.4.')), version
            assert 'STRICT' in connection.scalar(text('SELECT @@SESSION.sql_mode'))
        # Never drop an existing schema or use a populated database. Only
        # these newly created model tables are cleaned up after an owned run.
        assert inspect(engine).get_table_names() == [], 'Refusing a nonempty test schema'
        assert inspect(engine).get_view_names() == [], 'Refusing a nonempty test schema'
        try:
            yield engine
        finally:
            STOCK.drop(engine, checkfirst=True)
            HEAD.drop(engine, checkfirst=True)
            CashCowSnapshot.__table__.drop(engine, checkfirst=True)
    finally:
        engine.dispose()


def synthetic_snapshot(day=DAY):
    stocks = [
        dict(symbol='600036.SH', name='模拟银行'),
        dict(symbol='600900.SH', name='模拟非银行'),
    ]
    vals = {r['symbol']: dict(pe_ttm=7, total_mv=1000000, close=10, pb=1) for r in stocks}
    cash = {
        r['symbol']: {'fiscal_year': 2025, 'annual_dps': {2025: 0.5, 2024: 0.4, 2023: 0.3}}
        for r in stocks
    }
    industries = {
        '600036.SH': dict(l1_code='801780.SI', l1_name='Bank'),
        '600900.SH': dict(l1_code='801160.SI', l1_name='Utility'),
    }
    meta, rows = rank_records(stocks, vals, cash, industries, day, 5500)
    # Exact half ties at DECIMAL(24,6), including negative PB, and a JSON
    # double near a binary rounding boundary. No floating arithmetic for money.
    rows[0]['market_cap'] = Decimal('123.4567825')
    rows[0]['pb'] = Decimal('-1.2345675')
    rows[0]['dividend_yield_history'][1]['yield_pct'] = None
    rows[0]['dividend_yield_history'][2]['yield_pct'] = 7.123456789012345
    return meta, rows


def snapshot(executor):
    head = executor.execute(select(HEAD)).mappings().one()
    rows = executor.execute(select(STOCK).order_by(STOCK.c.method, STOCK.c.code)).mappings().all()
    return dict(head), [dict(row) for row in rows]


def remote_snapshot(engine):
    with engine.connect() as connection:
        return snapshot(connection)


def service_and_cache(engine, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / 'log',
        cache_enabled=True,
        cache_auto_refresh=False,
        db_host=HOST,
        db_port=engine.url.port,
        db_name=DATABASE,
        db_user=USER,
        db_password=PASSWORD,
    )
    config = dict(host=HOST, port=engine.url.port, database=DATABASE, revision=1)
    monkeypatch.setattr(dividend_collection, 'database_config', lambda _: config)
    # No market provider or Telegram/Feishu is contacted in this suite.
    monkeypatch.setattr(collection_notifications, 'emit_collection_result', lambda *a, **k: None)
    calls = []

    class SyntheticSource:
        def __init__(self, *a, **kw):
            assert kw['as_of'] == DAY

        def collect(self):
            calls.append(DAY)
            return synthetic_snapshot()

    cache = SnapshotCache(settings, engine)
    service = DividendCollection(
        settings,
        cache=cache,
        source_factory=SyntheticSource,
        engine_factory=lambda *a, **kw: engine,
        client_factory=lambda: nullcontext(None),
    )
    return service, cache, calls


def test_mysql_decimal_json_migration_and_cache_replay(mysql_engine, tmp_path, monkeypatch):
    engine = mysql_engine
    HEAD.create(engine)
    STOCK.create(engine)
    # Legacy schema width actually on MySQL, not an inspector mock.
    with engine.begin() as connection:
        connection.execute(
            text(
                'ALTER TABLE dividend_selection_snapshot MODIFY COLUMN rule_version VARCHAR(32) NOT NULL'
            )
        )

    service, cache, calls = service_and_cache(engine, tmp_path, monkeypatch)
    expected_meta, expected_rows = synthetic_snapshot()
    real_refresh = cache.refresh
    monkeypatch.setattr(cache, 'refresh', lambda *a, **kw: False)
    assert service.refresh(background=False, moment=MOMENT)
    assert calls == [DAY]
    assert service.status()['stage'] == 'publication_pending'
    assert service.pending() is not None
    first = remote_snapshot(engine)
    assert first[0]['rule_version'] == expected_meta['rule_version']
    width = next(
        c['type'].length
        for c in inspect(engine).get_columns(HEAD.name)
        if c['name'] == 'rule_version'
    )
    assert width >= len(expected_meta['rule_version'])
    assert records_match(first[1], expected_rows)
    stored = next(row for row in first[1] if row['code'] == expected_rows[0]['code'])
    assert stored['market_cap'] == Decimal('123.456783')
    assert stored['pb'] == Decimal('-1.234568')
    assert stored['dividend_yield_history'][1]['yield_pct'] is None
    expected_float = expected_rows[0]['dividend_yield_history'][2]['yield_pct']
    assert stored['dividend_yield_history'][2]['yield_pct'] in (
        expected_float,
        math.nextafter(expected_float, math.inf),
        math.nextafter(expected_float, -math.inf),
    )

    # Restart against the same durable pending payload, with the real cache
    # transfer and verification path. This is not a fake SQLite copy.
    monkeypatch.setattr(cache, 'refresh', real_refresh)
    restarted = DividendCollection(
        service.settings,
        cache=cache,
        engine_factory=lambda *a, **k: engine,
        client_factory=lambda: pytest.fail('Pending replay must not recollect'),
    )
    assert restarted.refresh(background=False, moment=MOMENT)
    assert restarted.status()['stage'] == 'complete'
    assert restarted.pending() is None
    assert remote_snapshot(engine) == first
    with cache.session('dividend-selection') as session:
        local_meta, local_rows = snapshot(session)
        assert session.bind.dialect.name == 'sqlite'
    assert local_meta == first[0]
    assert records_match(local_rows, expected_rows)
    assert records_match(local_rows, first[1])
    assert write_result(engine, expected_meta, expected_rows) == 0
    assert remote_snapshot(engine) == first


def test_mysql_transaction_rolls_back_both_groups_after_dml(mysql_engine):
    engine = mysql_engine
    meta, rows = synthetic_snapshot()
    assert write_result(engine, meta, rows) == len(rows)
    before = remote_snapshot(engine)
    assert {r['method'] for r in before[1]} == {'bank', 'value'}
    with engine.connect() as connection:
        engines = (
            connection.execute(
                text(
                    'SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() '
                    'AND TABLE_NAME IN (:head, :stock)'
                ),
                {'head': HEAD.name, 'stock': STOCK.name},
            )
            .scalars()
            .all()
        )
        assert len(engines) == 2 and all(e.upper() == 'INNODB' for e in engines)

    new_meta, new_rows = synthetic_snapshot(date(2026, 11, 17))
    for row in new_rows:
        row['name'] += '-replacement'
    reached = []

    def fail_before_commit(stage):
        if stage == '提交数据库事务':
            reached.append(stage)
            raise RuntimeError('synthetic abort after both groups written')

    # The failure occurs after actual DELETE/INSERT within the InnoDB
    # transaction, not merely before executing DML.
    with pytest.raises(RuntimeError, match='synthetic abort'):
        write_result(engine, new_meta, new_rows, progress=fail_before_commit)
    assert reached == ['提交数据库事务']
    assert remote_snapshot(engine) == before
    assert write_result(engine, new_meta, new_rows) == len(new_rows)
    after = remote_snapshot(engine)
    assert after[0]['trade_date'] == new_meta['trade_date']
    assert records_match(after[1], new_rows)
    with pytest.raises(DividendCollectionError, match='newer'):
        write_result(engine, meta, rows)
    assert remote_snapshot(engine) == after


def test_mysql_acknowledgement_loss_reconciles_without_rewriting(
    mysql_engine, tmp_path, monkeypatch
):
    engine = mysql_engine
    service, cache, calls = service_and_cache(engine, tmp_path, monkeypatch)
    real_write = dividend_collection.write_result
    attempts = []
    first = []

    def write_with_lost_ack(db_engine, meta, rows, *args, **kwargs):
        written = real_write(db_engine, meta, rows, *args, **kwargs)
        attempts.append(written)
        if len(attempts) == 1:
            # Real transaction has committed; inject only the client-observed
            # error, not an actual network fault or admin KILL command.
            first.append(remote_snapshot(engine))
            raise OperationalError(2013, 'synthetic lost acknowledgement')
        return written

    monkeypatch.setattr(dividend_collection, 'write_result', write_with_lost_ack)
    assert service.refresh(background=False, moment=MOMENT)
    assert service.status()['stage'] == 'complete'
    assert service.pending() is None
    assert calls == [DAY]
    assert attempts == [2, 0]
    assert remote_snapshot(engine) == first[0]
    assert service.status()['written'] is None  # Ambiguous commit must not fake a changed count.
    with cache.session('dividend-selection') as session:
        head, rows = snapshot(session)
    assert head == first[0][0]
    assert records_match(rows, first[0][1])


def test_mysql_legacy_schema_revisions_keep_business_rows_without_dml(mysql_engine):

    from app.services.dividend_collection import prepare_dividend_schema

    engine = mysql_engine
    meta, rows = synthetic_snapshot()
    write_result(engine, meta, rows)
    with engine.begin() as connection:
        connection.execute(HEAD.update().values(rule_version='legacy-v8'))
        connection.execute(STOCK.update().values(dividend_cash_1y=Decimal('123.456789')))
        connection.execute(
            text(
                'ALTER TABLE dividend_selection_snapshot MODIFY COLUMN rule_version VARCHAR(32) NOT NULL'
            )
        )
        connection.execute(
            text('ALTER TABLE dividend_selection_stock DROP COLUMN dividend_per_share')
        )
        connection.execute(
            text('ALTER TABLE dividend_selection_stock DROP COLUMN dividend_fiscal_year')
        )
        connection.execute(
            text(
                'ALTER TABLE dividend_selection_stock MODIFY COLUMN dividend_cash_1y NUMERIC(28,6) NOT NULL'
            )
        )
    with engine.connect() as connection:
        original_header = dict(connection.execute(select(HEAD)).mappings().one())
        original_rows = [
            dict(row)
            for row in connection.execute(
                text('SELECT * FROM dividend_selection_stock ORDER BY method,code')
            ).mappings()
        ]

    statements = []

    def observe(conn, cursor, statement, *_args):
        statements.append(statement.strip().split()[0].upper())

    event.listen(engine, 'before_cursor_execute', observe)
    try:
        prepare_dividend_schema(engine)
        assert statements.count('ALTER') == 4
        assert not set(statements) & {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'DROP'}
        statements.clear()
        prepare_dividend_schema(engine)
        assert 'ALTER' not in statements
        assert not set(statements) & {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'DROP'}
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    head, after = remote_snapshot(engine)
    assert head == original_header
    assert len(after) == len(original_rows)
    for original, migrated in zip(original_rows, after, strict=True):
        assert migrated['dividend_per_share'] is None
        assert migrated['dividend_fiscal_year'] is None
        # Raw SQL returns JSON as text on MySQL; compare typed columns directly.
        assert {k: migrated[k] for k in original if k != 'dividend_yield_history'} == {
            k: value for k, value in original.items() if k != 'dividend_yield_history'
        }
        assert migrated['dividend_yield_history'] == json.loads(original['dividend_yield_history'])


def test_mysql_cash_cow_transaction_replays_json_and_preserves_newer_snapshot(mysql_engine):
    writes = []

    def observe(conn, cursor, statement, *_args):
        if statement.startswith(('INSERT INTO cash_cow_snapshot', 'UPDATE cash_cow_snapshot')):
            writes.append(statement.split()[0])

    event.listen(mysql_engine, 'before_cursor_execute', observe)
    try:
        exercise_publication_contract(mysql_engine)
        assert writes == ['INSERT', 'UPDATE']  # Only initial insert and rolled-back update.
    finally:
        event.remove(mysql_engine, 'before_cursor_execute', observe)
