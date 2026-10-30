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
        # Adopt existing quarters once; the first local protocol sync establishes
        # a fresh baseline before trusting these versions.
        runs = Table('earnings_gap_run', MetaData(), Column('report_period', Date, primary_key=True))
        with engine.begin() as connection:
            periods = [row.report_period for row in connection.execute(runs.select())]
        for period in periods:
            try:
                with engine.begin() as connection:
                    if connection.execute(table.select().where(table.c.report_period == period)).first() is None:
                        connection.execute(table.insert().values(report_period=period,
                                           generation=_earnings_uuid.uuid4().hex, revision=0))
            except _EarningsIntegrityError:
                # A concurrent initializer may have adopted this quarter first.
                # Never overwrite its generation or a writer's advanced revision.
                with engine.begin() as connection:
                    if connection.execute(table.select().where(table.c.report_period == period)).first() is None:
                        raise
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

# END SHARED EARNINGS VERSION
