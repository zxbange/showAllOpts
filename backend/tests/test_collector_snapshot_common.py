"""Shared snapshot writes retain scope, idempotence and rollback guarantees."""
import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Date, Integer, String, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from test_collector_common import batch


DAY = dt.date(2026, 10, 26)
NEXT = DAY + dt.timedelta(days=1)


@pytest.fixture
def store():
    base = declarative_base()

    class Snapshot(base):
        __tablename__ = 'snapshot'
        series = Column(String, primary_key=True)
        day = Column(Date, primary_key=True)
        code = Column(String, primary_key=True)
        value = Column(Integer, nullable=False)

    engine = create_engine('sqlite://')
    base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    original = [
        dict(series='a', day=DAY, code='x', value=10),
        dict(series='a', day=DAY, code='y', value=20),
        dict(series='b', day=DAY, code='x', value=90),
        dict(series='a', day=NEXT, code='x', value=30),
    ]
    with Session() as session:
        session.add_all([Snapshot(**row) for row in original])
        session.commit()
    writes = []

    @event.listens_for(engine, 'before_cursor_execute')
    def collect_writes(connection, cursor, statement, parameters, context, executemany):
        operation = statement.lstrip().split(' ', 1)[0].upper()
        if operation in ('INSERT', 'UPDATE', 'DELETE'):
            writes.append(operation)

    def records():
        with Session() as session:
            return {(row.series, row.day, row.code): row.value for row in session.query(Snapshot).all()}

    result = SimpleNamespace(engine=engine, Session=Session, model=Snapshot,
        filters=(Snapshot.series == 'a', Snapshot.day == DAY),
        keys=('day', 'code'), records=records, writes=writes)
    try:
        yield result
    finally:
        engine.dispose()


def test_insert_only_filters_existing_keys_within_scope_and_repeat_has_no_writes(batch, store):
    existing = dict(series='a', day=DAY, code='x', value=999)
    added = dict(series='a', day=DAY, code='z', value=40)
    before = store.records()
    expected = dict(before)
    expected[('a', DAY, 'z')] = 40
    assert batch._save_record_set(store.Session, store.model, [existing, added],
                                  store.filters, store.keys) == [added]
    assert store.records() == expected
    assert store.writes == ['INSERT']
    store.writes.clear()
    assert batch._save_record_set(store.Session, store.model, [existing, added],
                                  store.filters, store.keys) == []
    assert store.writes == []


def test_replacement_deletes_only_requested_series_and_dates(batch, store):
    selected = [dict(series='a', day=DAY, code='z', value=40)]
    assert batch._save_record_set(store.Session, store.model, selected,
                                  store.filters, store.keys, skip_existing=False) == selected
    assert store.records() == {('a', DAY, 'z'): 40, ('b', DAY, 'x'): 90, ('a', NEXT, 'x'): 30}
    assert store.writes == ['DELETE', 'INSERT']


@pytest.mark.parametrize('skip_existing', [True, False])
def test_commit_failure_rolls_back_insert_and_scoped_deletion(batch, store, skip_existing):
    before = store.records()

    class FailingSession(store.Session.class_):
        def commit(self):
            self.flush()
            raise RuntimeError('commit failed')

    failing = sessionmaker(bind=store.engine, class_=FailingSession)
    with pytest.raises(RuntimeError, match='commit failed'):
        batch._save_record_set(failing, store.model,
            [dict(series='a', day=DAY, code='z', value=40)],
            store.filters, store.keys, skip_existing=skip_existing)
    assert store.records() == before


def test_empty_replacement_does_not_clear_existing_data_or_open_a_session(batch, store):
    before = store.records()

    def forbidden():
        pytest.fail('An empty collection must not open a write session')

    assert batch._save_record_set(forbidden, store.model, [],
                                  store.filters, store.keys, skip_existing=False) == []
    assert store.records() == before and store.writes == []
