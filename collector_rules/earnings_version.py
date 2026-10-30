"""Small quarterly versions distinguish append-only additions from full rebuilds."""
# BEGIN SHARED EARNINGS VERSION
import uuid as _earnings_uuid
from sqlalchemy import BigInteger, Column, Date, MetaData, String, Table


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
        table.create(engine, checkfirst=True)
        # Adopt existing quarters once; the first local protocol sync establishes
        # a fresh baseline before trusting these versions.
        runs = Table('earnings_gap_run', MetaData(), Column('report_period', Date, primary_key=True))
        with engine.begin() as connection:
            existing = {row.report_period for row in connection.execute(table.select())}
            for row in connection.execute(runs.select()):
                if row.report_period not in existing:
                    connection.execute(table.insert().values(report_period=row.report_period,
                                       generation=_earnings_uuid.uuid4().hex, revision=0))
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
