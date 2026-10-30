"""Small head comparison repairs whole partitions without historical detail scans."""
from datetime import date, datetime
from sqlalchemy import event, select
from app.models.short_term import ShortTermRun, ShortTermStock
from app.models.earnings import EarningsStock
from app.core.short_term_sync import MARKER
from app.core.earnings_sync import VERSION
from test_cache import cached
from test_cache_incremental import assert_matches
from test_earnings_api import seed


def test_short_same_second_and_legacy_inconsistent_details(cached, session, engine):
    _, cache = cached
    day, stamp = date(2026, 10, 22), datetime(2026, 10, 22, 17)
    session.add_all([ShortTermRun(trade_date=day, payload={'counts': {}}, updated_at=stamp),
                     ShortTermStock(trade_date=day, code='a', payload={'methods': []}, updated_at=stamp)])
    session.commit()
    cache.refresh('short-term', background=False)
    session.get(ShortTermRun, day).payload = {'counts': {'oversold': 1}}
    session.get(ShortTermStock, (day, 'a')).payload = {'methods': ['oversold']}
    session.commit()
    cache.refresh('short-term', background=False)
    assert_matches(cache, session, 'short-term')
    # Simulate an old snapshot whose header was fresh but details were stale.
    from sqlalchemy import create_engine
    path = cache.snapshot_path('short-term', cache.read_state()['modules']['short-term'])
    local = create_engine('sqlite:///' + str(path))
    with local.begin() as connection:
        MARKER.drop(connection)
        connection.execute(ShortTermStock.__table__.update().values(payload={'methods': []}))
    local.dispose()
    cache.refresh('short-term', background=False)
    assert_matches(cache, session, 'short-term')
    statements = []
    def capture(conn, cursor, sql, *args): statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try: cache.refresh('short-term', background=False)
    finally: event.remove(engine, 'before_cursor_execute', capture)
    assert not any('FROM short_term_stock' in sql for sql in statements)


def test_quarter_versions_skip_append_and_generation_rebuild(cached, session, engine):
    _, cache = cached
    period = date(2026, 9, 30)
    VERSION.create(engine)
    with engine.begin() as source:
        source.execute(VERSION.insert().values(report_period=period, generation='first', revision=0))
    cache.refresh('earnings-gap', background=False)
    statements = []
    def capture(conn, cursor, sql, *args): statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try: cache.refresh('earnings-gap', background=False)
    finally: event.remove(engine, 'before_cursor_execute', capture)
    assert not any('FROM earnings_gap_stock' in sql for sql in statements)
    seed(session, code='000001.XSHE')
    with engine.begin() as source:
        source.execute(VERSION.update().values(revision=1))
    cache.refresh('earnings-gap', background=False)
    assert_matches(cache, session, 'earnings-gap')
    session.query(EarningsStock).filter_by(code='600001.XSHG').delete()
    session.query(EarningsStock).filter_by(code='000001.XSHE').update({'name': 'rebuilt'})
    session.commit()
    with engine.begin() as source:
        source.execute(VERSION.update().values(generation='rebuilt', revision=2))
    cache.refresh('earnings-gap', background=False)
    assert_matches(cache, session, 'earnings-gap')
