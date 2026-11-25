"""Publication and cache verification share storage, not orchestration."""

from copy import deepcopy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_cash_cow_snapshot_repository import publication_payload
from test_dividend_collection import result

from app.core.cache import SnapshotCache
from app.core.cache_errors import SnapshotDataError
from app.models.cash_cow import CashCowSnapshot
from app.repositories.dividend_snapshot import read_snapshot, records_match
from app.services import cash_cow_import
from app.services.dividend_collection import write_result


def test_dividend_repository_reads_full_pinned_snapshot():
    engine = create_engine('sqlite://')
    try:
        with Session(engine) as session:
            meta, rows = result()
            assert write_result(engine, meta, rows) == 2
            head, stocks = read_snapshot(session)
            assert head is not None
            assert all(head[key] == value for key, value in meta.items())
            assert records_match(stocks, rows)
            before = deepcopy(head)
        assert write_result(engine, meta, rows) == 0
        with Session(engine) as session:
            head, stocks = read_snapshot(session)
            assert head == before
            assert records_match(stocks, rows)
        meta.update(value_count=0, value_candidates=0)
        assert write_result(engine, meta, []) == 1
        with Session(engine) as session:
            head, stocks = read_snapshot(session)
            assert head is not None and head['value_count'] == 0
            assert stocks == []
        assert write_result(engine, meta, []) == 0
    finally:
        engine.dispose()


def test_cache_validation_does_not_call_publication_service(monkeypatch):
    engine = create_engine('sqlite://')
    try:
        cash_cow_import.write_public(engine, publication_payload(), lambda: None)

        def reject_service_dependency(*args):
            pytest.fail('cache validation must not call cash-cow publication service')

        monkeypatch.setattr(cash_cow_import, 'validate_snapshot', reject_service_dependency)
        with engine.connect() as conn:
            # validate_module itself only validates the supplied target snapshot.
            SnapshotCache.validate_module(None, 'cash-cow', conn)
        with engine.begin() as conn:
            conn.execute(CashCowSnapshot.__table__.delete())
            with pytest.raises(SnapshotDataError):
                SnapshotCache.validate_module(None, 'cash-cow', conn)
    finally:
        engine.dispose()
