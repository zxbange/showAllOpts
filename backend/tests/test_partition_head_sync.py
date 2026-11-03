"""Small head comparison repairs whole partitions without historical detail scans."""
from datetime import date, datetime
from sqlalchemy import event, select
from app.models.earnings import EarningsStock, EarningsRun
from app.core.earnings_sync import VERSION
from test_cache import cached
from test_cache_incremental import assert_matches
from test_earnings_api import seed


def test_quarter_versions_skip_append_and_generation_rebuild(cached, session, engine):
    _, cache = cached
    period = date(2026, 9, 30)
    VERSION.create(engine)
    with engine.begin() as source:
        source.execute(VERSION.insert().values(report_period=period, generation='a'*32, revision=0))
    cache.refresh('earnings-gap', background=False)
    statements = []
    def capture(conn, cursor, sql, *args): statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try: cache.refresh('earnings-gap', background=False)
    finally: event.remove(engine, 'before_cursor_execute', capture)
    stock_reads = [sql for sql in statements if 'FROM earnings_gap_stock' in sql]
    assert len(stock_reads) == 1 and 'count(' in stock_reads[0].lower()
    assert 'events' not in stock_reads[0] and 'prices' not in stock_reads[0]
    seed(session, code='000001.XSHE')
    with engine.begin() as source:
        source.execute(VERSION.update().values(revision=1))
    cache.refresh('earnings-gap', background=False)
    assert_matches(cache, session, 'earnings-gap')
    session.query(EarningsStock).filter_by(code='600001.XSHG').delete()
    session.query(EarningsStock).filter_by(code='000001.XSHE').update({'name': 'rebuilt'})
    session.get(EarningsRun, period).matched_count = 1
    session.commit()
    with engine.begin() as source:
        source.execute(VERSION.update().values(generation='b'*32, revision=2))
    cache.refresh('earnings-gap', background=False)
    assert_matches(cache, session, 'earnings-gap')
