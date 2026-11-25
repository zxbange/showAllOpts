"""Exercise shared lease helpers at ETF and market service boundaries."""

from pathlib import Path

import portalocker
import pytest
from test_etf_collection import worker as etf_worker
from test_market_collection import worker as market_worker

from app.services.collection_lifecycle import hold_lease
from app.services.etf_configuration import ConfigurationFailure
from app.services.market_collection import CollectionBusy


def lock_for(path: Path) -> portalocker.Lock:
    return portalocker.Lock(str(path), timeout=0)


def test_context_lease_preserves_contention_and_body_exception(tmp_path):
    path = tmp_path / 'test.lock'
    with hold_lease(lock_for(path)) as first:
        with pytest.raises(portalocker.exceptions.LockException), hold_lease(lock_for(path)):
            pytest.fail('contended lock body executed')
        first.release()
        with pytest.raises(RuntimeError, match='body fault'), hold_lease(lock_for(path)):
            raise RuntimeError('body fault')
        with hold_lease(lock_for(path)):
            # Duplicate cleanup cannot release a newer owner's lock.
            first.release()
            with pytest.raises(portalocker.exceptions.LockException), hold_lease(lock_for(path)):
                pytest.fail('contended lock body executed')


def test_etf_probe_keeps_busy_failure_and_releases_after_probe_error(tmp_path, monkeypatch):
    service, collector, _ = etf_worker(tmp_path)
    service.root.mkdir(parents=True, exist_ok=True)
    calls = []

    def probe(body):
        calls.append(body)
        if len(calls) == 1:
            raise RuntimeError('synthetic probe failure')
        return {'ok': True}

    monkeypatch.setattr(service.configuration, 'test', probe)
    with hold_lease(lock_for(service.root / 'collection.lock')):
        with pytest.raises(ConfigurationFailure, match='采集正在运行'):
            service.test_configuration({})
    assert calls == []
    with pytest.raises(RuntimeError, match='probe failure'):
        service.test_configuration({})
    assert service.test_configuration({}) == {'ok': True}
    assert len(calls) == 2
    assert collector.calls == []  # Probe is not collection admission.


def test_market_request_replays_same_id_under_new_owner_but_never_enqueues_other_id(
    tmp_path,
):
    service, provider, writer, _ = market_worker(tmp_path)
    first = service._acquire()
    assert first is not None
    first.release()
    request_id = 'synthetic-request-same'
    accepted = service.request_run(request_id)
    assert accepted['status'] == 'queued'
    current = service._acquire()
    assert current is not None
    try:
        first.release()
        assert service.request_run(request_id)['id'] == request_id
        with pytest.raises(CollectionBusy):
            service.request_run('synthetic-other-request')
        with service.connect() as db:
            assert db.execute('SELECT id FROM manual_runs').fetchall()[0][0] == request_id
            assert db.execute('SELECT count(*) FROM manual_runs').fetchone()[0] == 1
    finally:
        current.release()
    assert not writer.calls
