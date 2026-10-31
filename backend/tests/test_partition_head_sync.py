"""Small head comparison repairs whole partitions without historical detail scans."""
from datetime import date, datetime
from sqlalchemy import event, select
from app.models.short_term import ShortTermRun, ShortTermStock
from app.models.earnings import EarningsStock, EarningsRun
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


from test_short_term_collector import mod, ns, complete_meta


def test_collector_same_second_repair_replaces_cached_details(cached, session, engine, ns, mod, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    _, cache = cached
    stamp = datetime(2026, 10, 22, 17)
    monkeypatch.setattr(mod, 'china_now', lambda: stamp)
    day = stamp.date()
    meta = complete_meta(ns)
    meta['counts']['oversold'] = 1
    session.add_all([ShortTermRun(trade_date=day, payload=meta, updated_at=stamp),
                    ShortTermStock(trade_date=day, code='a', payload={'methods': ['bukou']}, updated_at=stamp)])
    session.commit()
    cache.refresh('short-term', background=False)
    assert ns.save_day(sessionmaker(bind=engine), day, meta, [{'code': 'a', 'methods': ['oversold']}])
    cache.refresh('short-term', background=False)
    session.expire_all()
    assert_matches(cache, session, 'short-term')
    with cache.session('short-term') as local:
        assert local.get(ShortTermStock, (day, 'a')).payload['methods'] == ['oversold']
        assert local.get(ShortTermRun, day).updated_at == stamp
        assert local.get(ShortTermRun, day).payload['snapshot_revision']
