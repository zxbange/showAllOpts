"""Retiring methods must preserve remaining signals and stop obsolete work."""
import datetime as dt
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from test_short_term_collector import mod, ns


def seed_legacy(ns, Session, *, corrupt=False):
    day = dt.date(2026, 10, 30)
    stamp = dt.datetime(2026, 10, 30, 18)
    counts = {'bukou': 1, 'oversold': 1, 'retired_a': 2, 'retired_b': 1}
    if corrupt:
        counts['bukou'] = 2
    with Session.begin() as session:
        session.add(ns.ShortTermRun(trade_date=day, updated_at=stamp, payload={
            'rule_version': 'stock_four_patterns_v3_oversold', 'counts': counts,
            'watch_version': ns.WATCH_VERSION, 'quotes_as_of': str(day), 'snapshot_revision': 'legacy',
        }))
        for code, methods in [('a', ['bukou', 'retired_a']), ('b', ['retired_a', 'retired_b']), ('c', ['oversold'])]:
            session.add(ns.ShortTermStock(trade_date=day, code=code, updated_at=stamp,
                payload={'methods': methods, 'close': 10, 'quote_close': 12, 'bars': [{'close': 12}]}))
    return day


def test_legacy_pruning_preserves_signals_and_is_idempotent(ns):
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    try:
        day = seed_legacy(ns, Session)
        with Session.begin() as session:
            ns.migrate_methods(session)
            head = session.get(ns.ShortTermRun, day)
            assert ns.complete_day(session, head)
            assert head.payload['counts'] == {'bukou': 1, 'oversold': 1}
            assert head.payload['snapshot_revision'] != 'legacy'
            meta = deepcopy(head.payload)
            rows = session.query(ns.ShortTermStock).order_by(ns.ShortTermStock.code).all()
            assert [r.code for r in rows] == ['a', 'c']
            assert rows[0].payload == {'methods': ['bukou'], 'close': 10, 'quote_close': 12, 'bars': [{'close': 12}]}
        # No quote APIs available: both migration and quote refresh must become no-ops.
        with Session.begin() as session:
            ns.migrate_methods(session)
            assert session.get(ns.ShortTermRun, day).payload == meta
        assert ns.refresh_quotes(Session, [day]) == 0
    finally:
        engine.dispose()


def test_incomplete_legacy_snapshot_is_not_certified_as_complete(ns):
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    try:
        day = seed_legacy(ns, Session, corrupt=True)
        with Session.begin() as session:
            ns.migrate_methods(session)
            head = session.get(ns.ShortTermRun, day)
            assert head.payload['snapshot_revision'] == 'legacy'
            assert not ns.complete_day(session, head)
            assert session.query(ns.ShortTermStock).count() == 3
    finally:
        engine.dispose()


def test_pruning_failure_rolls_back_memberships_and_header(ns):
    engine = create_engine('sqlite://')
    ns.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    try:
        day = seed_legacy(ns, Session)
        def fail(conn, cursor, statement, *args):
            if statement.startswith('UPDATE short_term_run'):
                raise RuntimeError('simulated write failure')
        event.listen(engine, 'before_cursor_execute', fail)
        with pytest.raises(RuntimeError, match='simulated'):
            with Session.begin() as session:
                ns.migrate_methods(session)
        event.remove(engine, 'before_cursor_execute', fail)
        with Session() as session:
            assert session.get(ns.ShortTermRun, day).payload['snapshot_revision'] == 'legacy'
            assert session.query(ns.ShortTermStock).count() == 3
            assert session.get(ns.ShortTermStock, (day, 'a')).payload['methods'] == ['bukou', 'retired_a']
    finally:
        engine.dispose()


def test_local_sync_filters_legacy_source_and_reuses_unchanged_headers(ns):
    from app.core.short_term_sync import sync_short_term
    source, target = create_engine('sqlite://'), create_engine('sqlite://')
    tables = [ns.ShortTermRun.__table__, ns.ShortTermStock.__table__]
    try:
        for engine in (source, target):
            ns.Base.metadata.create_all(engine)
        day = seed_legacy(ns, sessionmaker(bind=source))
        with source.connect() as remote, target.begin() as local:
            sync_short_term(remote, local, tables, False)
            rows = local.execute(select(tables[1]).order_by(tables[1].c.code)).mappings().all()
            assert [r['code'] for r in rows] == ['a', 'c']
            assert rows[0]['payload']['methods'] == ['bukou']
            assert local.execute(select(tables[0].c.payload)).scalar()['counts'] == {'bukou': 1, 'oversold': 1}
        reads = []
        def record(conn, cursor, sql, *args):
            if 'short_term_stock' in sql:
                reads.append(sql)
        event.listen(source, 'before_cursor_execute', record)
        with source.connect() as remote, target.begin() as local:
            sync_short_term(remote, local, tables, True)
        assert reads == []
        # The source stays read-only and may be upgraded by the collector later.
        with source.connect() as remote:
            assert remote.execute(select(tables[0].c.payload)).scalar()['snapshot_revision'] == 'legacy'
    finally:
        source.dispose()
        target.dispose()


def test_api_rejects_retired_methods_and_hides_legacy_tags(client, session):
    from app.models.short_term import ShortTermRun, ShortTermStock
    day, stamp = dt.date(2026, 10, 30), dt.datetime(2026, 10, 30, 18)
    session.add(ShortTermRun(trade_date=day, updated_at=stamp,
        payload={'counts': {'bukou': 1, 'oversold': 0, 'retired_a': 1}}))
    session.add(ShortTermStock(trade_date=day, code='a', updated_at=stamp,
        payload={'methods': ['bukou', 'retired_a']}))
    session.commit()
    data = client.get('/api/v1/short-term/latest').json()
    assert data['counts'] == data['meta']['counts'] == {'bukou': 1, 'oversold': 0}
    assert data['items'][0]['methods'] == ['bukou']
    assert client.get('/api/v1/short-term/latest?method=retired_a').status_code == 422
