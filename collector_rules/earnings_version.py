"""Small quarterly versions distinguish append-only additions from full rebuilds."""
# BEGIN SHARED EARNINGS VERSION
import uuid as _earnings_uuid
from sqlalchemy import BigInteger, Column, Date, MetaData, String, Table
from sqlalchemy.schema import CreateTable as _EarningsCreateTable
from sqlalchemy import text as _earnings_text
from sqlalchemy.exc import IntegrityError as _EarningsIntegrityError


def _earnings_version_table():
    table = Table('earnings_sync_head', MetaData(),
                  Column('report_period', Date, primary_key=True),
                  Column('generation', String(32), nullable=False),
                  Column('revision', BigInteger, nullable=False))
    table.dialect_options['mysql']['engine'] = 'InnoDB'
    table.dialect_options['mysql']['charset'] = 'utf8mb4'
    return table


def _prepare_earnings_version(Session):
    if not getattr(Session, '_earnings_version_ready', False):
        table = _earnings_version_table()
        engine = Session.kw['bind']
        # Database-side idempotence works on older SQLAlchemy used in notebooks.
        ddl = str(_EarningsCreateTable(table).compile(dialect=engine.dialect))
        with engine.begin() as connection:
            connection.execute(_earnings_text(ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)))
        marker = _earnings_migration_table()
        ddl = str(_EarningsCreateTable(marker).compile(dialect=engine.dialect))
        with engine.begin() as connection:
            connection.execute(_earnings_text(ddl.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)))
        if engine.dialect.name == 'mysql':
            with engine.connect() as connection:
                engines = connection.execute(_earnings_text("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('earnings_gap_run','earnings_gap_stock','earnings_sync_head','earnings_sync_migration')")).fetchall()
            if len(engines) != 4 or any(str(row[1]).lower() != 'innodb' for row in engines):
                raise ValueError('净利润断层四张数据/版本表必须使用InnoDB；请先检查表引擎，拒绝非事务发布。')
        # DDL setup does not adopt/publish unvalidated historical quarters.
        # Check both schemas now; migration will create versions transactionally.
        with engine.connect() as connection:
            connection.execute(table.select().limit(0))
            connection.execute(marker.select().limit(0))
        Session._earnings_version_ready = True


def _lock_earnings_version(session, period):
    table = _earnings_version_table()
    row = session.execute(table.select().where(table.c.report_period == period).with_for_update()).first()
    if row is None:
        session.execute(table.insert().values(report_period=period, generation=_earnings_uuid.uuid4().hex, revision=0))
    return table


def _advance_earnings_version(session, table, period, rebuild=False):
    values = {'revision': table.c.revision + 1}
    if rebuild:
        values['generation'] = _earnings_uuid.uuid4().hex
    session.execute(table.update().where(table.c.report_period == period).values(**values))

def _earnings_migration_table():
    table = Table('earnings_sync_migration', MetaData(),
                  Column('report_period', Date, primary_key=True),
                  Column('protocol', BigInteger, nullable=False))
    table.dialect_options['mysql']['engine'] = 'InnoDB'
    table.dialect_options['mysql']['charset'] = 'utf8mb4'
    return table


def _verify_earnings_quarter(session, period):
    # Read only scalar metadata/counts. Do not copy large JSON payloads or
    # infer completeness by rewriting matched_count to whatever remains.
    run = session.execute(_earnings_text(
        'SELECT as_of_date, universe_count, matched_count, rule_version FROM earnings_gap_run WHERE report_period=:period'),
        {'period': period}).first()
    count = session.execute(_earnings_text(
        'SELECT COUNT(*) FROM earnings_gap_stock WHERE report_period=:period'), {'period': period}).scalar()
    if (run is None or run[0] is None or str(run[0])[:10] < str(period)[:10]
            or run[1] is None or run[2] is None or run[1] <= 0
            or run[2] < 0 or run[2] > run[1] or not run[3] or run[2] != count):
        declared = run[2] if run is not None and isinstance(run[2], int) else 'unknown'
        raise ValueError('季度 {} 数据不完整：声明数量={}，实际数量={}；未发布新版本，请核查后重新采集该季度。'.format(period, declared, count))
    invalid = session.execute(_earnings_text("""SELECT COUNT(*) FROM earnings_gap_stock
        WHERE report_period=:period AND (code IS NULL OR code='' OR name IS NULL
        OR signal_date IS NULL OR announcement_date IS NULL OR valuation_date IS NULL
        OR announcement_date>signal_date OR signal_date>valuation_date OR valuation_date>:as_of
        OR signal_source NOT IN ('formal','forecast','express') OR signal_source IS NULL
        OR profit_yoy IS NULL OR profit_yoy<20 OR open_gap_pct IS NULL OR open_gap_pct<3
        OR intraday_pct IS NULL OR intraday_pct<0 OR events IS NULL OR prices IS NULL)"""),
        {'period': period, 'as_of': run[0]}).scalar()
    if invalid:
        raise ValueError('季度 {} 有 {} 条不完整或无效股票记录；未发布新版本，请核查后重新采集该季度。'.format(period, invalid))
    return count


def _migrate_earnings_versions(Session):
    """One validated generation reset per existing quarter, including history.

    Never delete business rows or repair missing rows by lowering the head.
    Marker and generation are committed together under the existing row lock.
    """
    _prepare_earnings_version(Session)
    engine = Session.kw['bind']
    with engine.connect() as connection:
        orphan = connection.execute(_earnings_text('SELECT COUNT(*) FROM earnings_gap_stock s LEFT JOIN earnings_gap_run r ON r.report_period=s.report_period WHERE r.report_period IS NULL')).scalar()
        if orphan:
            raise ValueError('净利润断层存在没有季度元数据的股票记录；拒绝迁移，请先核查对应季度。')
        runs = Table('earnings_gap_run', MetaData(), Column('report_period', Date, primary_key=True))
        marker = _earnings_migration_table()
        periods = [row[0] for row in connection.execute(runs.select().select_from(runs.outerjoin(marker, runs.c.report_period == marker.c.report_period)).where((marker.c.report_period.is_(None)) | (marker.c.protocol < 2)).order_by(runs.c.report_period))]
    marker = _earnings_migration_table()
    for period in periods:
        session = Session()
        try:
            table = _lock_earnings_version(session, period)
            migrated = session.execute(marker.select().where(marker.c.report_period == period).with_for_update()).first()
            if migrated is not None and migrated.protocol >= 2:
                continue
            _verify_earnings_quarter(session, period)
            _advance_earnings_version(session, table, period, rebuild=True)
            if migrated is None:
                session.execute(marker.insert().values(report_period=period, protocol=2))
            else:
                session.execute(marker.update().where(marker.c.report_period == period).values(protocol=2))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

# END SHARED EARNINGS VERSION
