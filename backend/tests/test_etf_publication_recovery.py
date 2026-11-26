"""Committed ETF data stays pending until the actual API snapshot is verified."""

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_etf_api import row
from test_etf_page_configuration import save
from test_etf_sse_scoped_worker import HISTORY, TARGET, SourceFixture

from app.api.routes.etf import router
from app.core import database
from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.models.etf import ETFPrice, ETFShare, ETFTradingDay
from app.repositories.etf_publication_state import clear_publication_requirements, mark_committed
from app.services import collection_outcomes
from app.services.etf_collection import ETFCollection


def _history_client(cache, worker):
    app = FastAPI()
    app.state.cache = cache
    app.state.etf_collection = worker
    app.include_router(router, prefix='/api/v1')
    return TestClient(app)


def _history(client):
    response = client.get('/api/v1/etf-shares/history?group=hs300')
    assert response.status_code == 200 and response.headers['X-Data-Source'] == 'local-cache'
    return response.json()


def _status(client):
    response = client.get('/api/v1/etf-shares/collection-status')
    assert response.status_code == 200
    return response.json()


def test_actual_worker_cache_lock_source_mismatch_and_verified_api_recovery(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    save(case.worker, {'enabled': False, 'schedule': ['08:00']})
    case.worker.cache_publisher = case.cache.publish_etf_collection
    case.worker.cache_verifier = case.cache.verify_etf_publication
    case.seed_ledger(TARGET)
    code = '510300.XSHG'
    case.seed_share(code, HISTORY, 12_345)
    assert case.cache.publish_etf_collection().successful
    prior_file = case.cache.read_state()['modules']['etf-shares']['file']
    client = _history_client(case.cache, case.worker)
    assert _history(client)['end_date'] == str(HISTORY)
    held = case.cache.acquire()
    assert held is not None
    try:
        case.worker.request_run('cache-publication-recovery')
        assert case.worker.tick(case.now)  # MySQL analogue committed despite another cache user.
        state = _status(client)
        assert state['cache_pending'] and state['cache_publication']['status'] == 'waiting_lock'
        assert state['cache_publication']['required_through'] == str(TARGET)
        assert state['latest_result']['status'] == 'partial'
        original = state['latest_result']
        assert (
            collection_outcomes.latest_outcomes(case.worker.settings)['items']['etf-shares'][
                'outcome'
            ]
            == 'partial'
        )
        assert _history(client)['end_date'] == str(HISTORY)
    finally:
        held.release()

    case.worker.client_factory = lambda: pytest.fail('cache-only retry must not fetch exchanges')
    table = ETFShare.__table__
    condition = (table.c.fund_code == code) & (table.c.trade_date == TARGET)
    with case.engine.begin() as source:
        committed = dict(source.execute(select(table).where(condition)).mappings().one())
        # Simulate an inconsistent reading source: one acknowledged key is absent.
        source.execute(table.delete().where(condition))
    case.now += timedelta(minutes=6)
    assert not case.worker.tick(case.now)
    state = _status(client)
    assert state['cache_pending'] and state['cache_publication']['status'] == 'failed'
    assert state['cache_publication']['error_code'] == 'SOURCE_DATA_INCOMPLETE'
    assert case.cache.read_state()['modules']['etf-shares']['file'] == prior_file
    assert _history(client)['end_date'] == str(HISTORY)
    assert not any(key.startswith('cache_expected_day:') for key in state['metadata'])
    assert (
        collection_outcomes.latest_outcomes(case.worker.settings)['items']['etf-shares']['outcome']
        == 'partial'
    )

    with case.engine.begin() as source:
        source.execute(table.insert(), committed)
    case.now += timedelta(minutes=6)
    assert not case.worker.tick(case.now)
    state = _status(client)
    assert not state['cache_pending'] and state['cache_publication']['status'] == 'published'
    assert state['cache_publication']['latest_share_date'] == str(TARGET)
    assert state['cache_publication']['latest_price_date'] == str(TARGET)
    history = _history(client)
    assert history['end_date'] == str(TARGET)
    assert history['funds'][0]['shares'] == str(committed['shares'])
    assert len(state['recent_runs']) == 1  # A publication-only retry is not another collection.
    recovered = state['latest_result']
    assert recovered['id'] == original['id'] and recovered['finished_at'] == original['finished_at']
    assert recovered['status'] == 'complete' and recovered['error_details'] is None
    assert recovered['publication_recovered_at']
    unified = collection_outcomes.latest_outcomes(case.worker.settings)['items']['etf-shares']
    assert (
        unified['outcome'] == 'success'
        and unified['publication_recovered_at'] == recovered['publication_recovered_at']
    )
    with case.worker.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM metadata WHERE key LIKE 'cache_expected_day:%'"
        ).fetchone()
        assert (
            db.execute('SELECT status FROM run_history WHERE id=?', (original['id'],)).fetchone()[0]
            == 'partial'
        )
        raw = json.loads(
            db.execute(
                'SELECT details FROM run_history_errors WHERE id=?', (original['id'],)
            ).fetchone()[0]
        )
        assert raw['code'] == 'publication_pending' and raw['publication_synced']
    client.close()


def _restarted_worker(case):
    worker = ETFCollection(
        case.worker.settings,
        cache_publisher=case.cache.publish_etf_collection,
        cache_verifier=case.cache.verify_etf_publication,
        collector=case.worker.collector,
        clock=lambda: case.now,
        client_factory=lambda: pytest.fail('publication-only recovery must not fetch an exchange'),
    )
    worker.start = lambda: None
    return worker


@pytest.mark.parametrize('missing_key', [False, True])
@pytest.mark.parametrize('manual', [False, True])
def test_existing_old_red_ledger_requires_actual_same_date_snapshot(
    tmp_path, monkeypatch, missing_key, manual
):
    from app.core import cache as cache_module

    case = SourceFixture(tmp_path, monkeypatch, manual=manual)
    monkeypatch.setattr(cache_module, 'now', lambda: case.now)
    case.worker.refresh = lambda **_: False  # Old collector could not acquire refresh.lock.
    if manual:
        save(case.worker, {'enabled': False, 'schedule': ['08:00']})
        case.seed_ledger(TARGET)
        case.worker.request_run('old-cache-pending-manual')
    assert case.worker.tick(case.now)
    old = case.worker.status()['latest_result']
    assert old['status'] == 'partial' and old['error_details']['code'] == 'publication_pending'
    # Reproduce the old deployed ledger: no per-run requirement or recovery
    # receipt, and cache_pending was cleared after the old boolean refresh.
    with case.worker.connect() as db:
        db.execute(
            'UPDATE run_history_errors SET details=? WHERE id=?',
            (json.dumps({'code': 'publication_pending', 'stage': 'cache_publish'}), old['id']),
        )
    case.now += timedelta(minutes=6)
    table = ETFPrice.__table__
    condition = (table.c.fund_code == '510050.XSHG') & (table.c.trade_date == TARGET)
    if missing_key:
        with case.engine.begin() as source:
            missing = dict(source.execute(select(table).where(condition)).mappings().one())
            source.execute(table.delete().where(condition))
    assert case.cache.refresh_etf_collection()
    with case.worker.connect() as db:
        clear_publication_requirements(db)
    restarted = _restarted_worker(case)
    assert not restarted.tick(case.now)
    if missing_key:
        # Other funds already give the cache MAX(date)=TARGET; that is not
        # sufficient proof of all committed observations.
        assert restarted.status()['latest_result']['status'] == 'partial'
        assert (
            collection_outcomes.latest_outcomes(restarted.settings)['items']['etf-shares'][
                'outcome'
            ]
            == 'partial'
        )
        with case.engine.begin() as source:
            source.execute(table.insert(), missing)
        case.now += timedelta(minutes=1)
        assert case.cache.refresh_etf_collection()
        assert not restarted.tick(case.now)
    result = restarted.status()['latest_result']
    assert result['id'] == old['id'] and result['finished_at'] == old['finished_at']
    assert result['status'] == 'complete' and result['publication_recovered_at']
    assert (
        collection_outcomes.latest_outcomes(restarted.settings)['items']['etf-shares']['outcome']
        == 'success'
    )
    assert len(restarted.status()['recent_runs']) == 1


def test_legacy_recovery_cannot_use_a_different_database_target(tmp_path, monkeypatch):
    from app.core import cache as cache_module

    case = SourceFixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cache_module, 'now', lambda: case.now)
    case.worker.refresh = lambda **_: False
    assert case.worker.tick(case.now)
    old = case.worker.status()['latest_result']
    with case.worker.connect() as db:
        db.execute(
            'UPDATE run_history_errors SET details=? WHERE id=?',
            (json.dumps({'code': 'publication_pending', 'stage': 'cache_publish'}), old['id']),
        )
    case.now += timedelta(minutes=6)
    assert case.cache.refresh_etf_collection()
    with case.worker.connect() as db:
        clear_publication_requirements(db)
    # The original snapshot has the correct dates, but it no longer belongs
    # to the database selected for the collection ledger/API.
    save(case.worker, {'enabled': False, 'database': 'different_market_target'})
    restarted = _restarted_worker(case)
    assert not restarted.tick(case.now)
    result = restarted.status()['latest_result']
    assert result['id'] == old['id'] and result['status'] == 'partial'
    assert (
        collection_outcomes.latest_outcomes(restarted.settings)['items']['etf-shares']['outcome']
        == 'partial'
    )


def test_later_real_failure_is_not_hidden_by_older_publication_recovery(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch, manual=True)
    save(case.worker, {'enabled': False, 'schedule': ['08:00']})
    case.seed_ledger(TARGET)
    case.worker.cache_publisher = case.cache.publish_etf_collection
    case.worker.cache_verifier = case.cache.verify_etf_publication
    held = case.cache.acquire()
    assert held is not None
    try:
        case.worker.request_run('earlier-publication-pending')
        assert case.worker.tick(case.now)
        old = case.worker.status()['latest_result']
        assert old['status'] == 'partial'
        case.now += timedelta(minutes=1)

        def unavailable():
            raise RuntimeError('fixture unavailable')

        case.worker.client_factory = unavailable
        case.worker.request_run('later-source-failure')
        assert not case.worker.tick(case.now)
        assert case.worker.status()['latest_result']['status'] == 'failed'
    finally:
        held.release()
    restarted = _restarted_worker(case)
    case.now += timedelta(minutes=6)
    assert not restarted.tick(case.now)
    state = restarted.status()
    assert not state['cache_pending']
    assert state['latest_result']['status'] == 'failed'
    older = next(row for row in state['recent_runs'] if row['id'] == old['id'])
    assert older['status'] == 'complete' and older['publication_recovered_at']
    unified = collection_outcomes.outcome_history(restarted.settings, 'etf-shares')['items']
    assert [row['outcome'] for row in unified] == ['failed', 'success']
    assert unified[1]['run_key'] == old['id']


def test_real_source_gap_stays_partial_after_cache_only_success(tmp_path, monkeypatch):
    import httpx

    case = SourceFixture(tmp_path, monkeypatch)
    case.worker.cache_publisher = case.cache.publish_etf_collection
    case.worker.cache_verifier = case.cache.verify_etf_publication
    case.overrides[('510300', TARGET)] = lambda request: httpx.Response(200, json={'result': []})
    held = case.cache.acquire()
    assert held is not None
    try:
        assert not case.worker.tick(case.now)
        old = case.worker.status()['latest_result']
        assert old['status'] == 'partial'
        assert old['error_details']['code'] != 'publication_pending'
    finally:
        held.release()
    restarted = _restarted_worker(case)
    case.now += timedelta(minutes=6)
    assert not restarted.tick(case.now)
    state = restarted.status()
    assert not state['cache_pending']
    assert state['latest_result']['status'] == 'partial'
    assert 'publication_recovered_at' not in state['latest_result']
    assert (
        collection_outcomes.latest_outcomes(restarted.settings)['items']['etf-shares']['outcome']
        == 'partial'
    )


def test_recovery_receipt_survives_failed_shared_projection_without_duplicate_run(
    tmp_path, monkeypatch
):
    case = SourceFixture(tmp_path, monkeypatch)
    case.worker.cache_publisher = case.cache.publish_etf_collection
    case.worker.cache_verifier = case.cache.verify_etf_publication
    held = case.cache.acquire()
    assert held is not None
    try:
        assert case.worker.tick(case.now)
    finally:
        held.release()
    real = collection_outcomes.recover_publication_outcome
    monkeypatch.setattr(
        collection_outcomes, 'recover_publication_outcome', lambda *args, **kwargs: False
    )
    restarted = _restarted_worker(case)
    case.now += timedelta(minutes=6)
    assert not restarted.tick(case.now)
    state = restarted.status()
    assert state['latest_result']['status'] == 'complete' and not state['cache_pending']
    assert (
        collection_outcomes.latest_outcomes(restarted.settings)['items']['etf-shares']['outcome']
        == 'partial'
    )
    monkeypatch.setattr(collection_outcomes, 'recover_publication_outcome', real)
    again = _restarted_worker(case)
    case.now += timedelta(minutes=1)
    assert not again.tick(case.now)
    assert (
        collection_outcomes.latest_outcomes(again.settings)['items']['etf-shares']['outcome']
        == 'success'
    )
    assert len(collection_outcomes.outcome_history(again.settings, 'etf-shares')['items']) == 1
    # A separately restored shared ledger may predate its successful repair.
    # The local immutable recovery receipt must still win for this same run.
    with collection_outcomes.database(again.settings) as outcomes:
        outcomes.execute("UPDATE results SET outcome='partial' WHERE module='etf-shares'")
        outcomes.execute(
            "UPDATE result_details SET payload=? WHERE module='etf-shares'",
            (json.dumps({'code': 'publication_pending', 'stage': 'cache_publish'}),),
        )
    case.now += timedelta(minutes=1)
    assert not again.tick(case.now)
    assert (
        collection_outcomes.latest_outcomes(again.settings)['items']['etf-shares']['outcome']
        == 'success'
    )


def test_expected_older_day_forces_dirty_window_and_actual_snapshot_check(tmp_path):
    source = create_engine('sqlite:///' + str(tmp_path / 'source.sqlite'))
    ETFShare.metadata.create_all(
        source, tables=[ETFShare.__table__, ETFPrice.__table__, ETFTradingDay.__table__]
    )
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / 'log',
        cache_enabled=True,
        db_user='test-reader',
        db_password='test-only',
    )
    cache = SnapshotCache(settings, source)
    collector = ETFCollection(settings, cache_publisher=cache.publish_etf_collection)
    days = [date(2026, 11, n) for n in (13, 16, 17, 18, 19, 20, 23, 24, 25)]
    old = days[0]
    with Session(source) as session:
        session.add_all(row(day, 10) for day in days[1:])
        session.commit()
    assert cache.publish_etf_collection().successful
    with Session(source) as session:
        session.add(row(old, 99))
        session.commit()
    assert (
        cache.refresh_etf_collection()
    )  # Legacy recent-only policy demonstrably misses the old write.
    with cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', old)) is None
    with collector.connect() as db:
        mark_committed(db, old, old, {('shares', '510300.XSHG', old)})
    result = collector.retry_cache_publication()
    assert result.successful and not collector.status()['cache_pending']
    with cache.session('etf-shares') as local:
        assert local.get(ETFShare, ('510300.XSHG', old)).shares == 99
    source.dispose()


def test_old_pending_target_survives_a_new_automatic_collection(tmp_path, monkeypatch):
    case = SourceFixture(tmp_path, monkeypatch)
    case.worker.cache_publisher = case.cache.publish_etf_collection
    code = '510300.XSHG'
    prior = HISTORY
    with case.worker.connect() as db:
        db.executemany(
            'INSERT OR REPLACE INTO metadata VALUES (?,?)',
            [('cache_pending', '1'), ('coverage_end', str(prior))],
        )
        db.execute(
            "INSERT INTO observations(kind,code,day,status) VALUES ('shares',?,?,'complete')",
            (code, str(prior)),
        )
    # Legacy ledger says prior was committed, but the reading source cannot
    # return it. New automatic collection must not replace that obligation.
    assert case.worker.tick(case.now)
    assert case.worker.status()['cache_pending']
    with case.worker.connect() as db:
        assert db.execute(
            'SELECT value FROM metadata WHERE key=?', ('cache_expected_day:' + prior.isoformat(),)
        ).fetchone()
        assert db.execute("SELECT value FROM metadata WHERE key='coverage_end'").fetchone()[
            0
        ] == str(TARGET)
    case.worker.client_factory = lambda: pytest.fail('pending publication must not re-fetch')
    case.now += timedelta(minutes=6)
    assert not case.worker.tick(case.now)
    state = case.worker.status()
    assert (
        state['cache_pending']
        and state['cache_publication']['error_code'] == 'SOURCE_DATA_INCOMPLETE'
    )
    case.seed_share(code, prior, 12_345)
    case.now += timedelta(minutes=6)
    assert not case.worker.tick(case.now)
    assert not case.worker.status()['cache_pending']
    with case.cache.session('etf-shares') as local:
        assert local.get(ETFShare, (code, prior)).shares == 12_345
        assert local.get(ETFShare, (code, TARGET)) is not None


def test_cli_etf_uses_api_namespace_verifies_existing_pending_and_exit_status(
    tmp_path, monkeypatch, capsys
):
    source = create_engine('sqlite:///' + str(tmp_path / 'source.sqlite'))
    ETFShare.metadata.create_all(
        source, tables=[ETFShare.__table__, ETFPrice.__table__, ETFTradingDay.__table__]
    )
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / 'log',
        cache_enabled=True,
        db_user='test-reader',
        db_password='test-only',
    )
    accesses = []

    def build(*_args, for_cache=False, **_kwargs):
        accesses.append(for_cache)
        return source

    monkeypatch.setattr(database, 'build_engine', build)
    api_cache = SnapshotCache(settings, database.VersionedDatabase(settings, for_cache=True))
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'refresh_cache.py'
    spec = importlib.util.spec_from_file_location('etf_cache_recovery_cli', path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'Settings', lambda: settings)
    monkeypatch.setattr(cli, 'configure_logging', lambda _config: None)
    monkeypatch.setenv('APP_ENV_FILE', str(tmp_path / 'unused-config.toml'))
    assert cli.main(['--module', 'etf-shares', '--status']) == 0
    assert [item['id'] for item in json.loads(capsys.readouterr().out)['modules']] == ['etf-shares']
    assert not accesses
    prior, target = date(2026, 11, 24), date(2026, 11, 25)
    code = '510300.XSHG'
    with Session(source) as session:
        session.add(row(prior, 10))
        session.commit()
    # External historical import can precede the first application collection.
    # The recovery tool must safely initialize its own local lock/ledger.
    assert not (settings.log_path / 'market-data' / 'etf-collection').exists()
    assert cli.main(['--module', 'etf-shares', '--full']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'published' and receipt['latest_share_date'] == str(prior)
    with api_cache.session('etf-shares') as local:
        assert local.get(ETFShare, (code, prior)).shares == 10
    initial = api_cache.read_state()['modules']['etf-shares']['file']
    collector = ETFCollection(settings)
    with collector.connect() as db:
        db.executemany(
            'INSERT OR REPLACE INTO metadata VALUES (?,?)',
            [
                ('cache_pending', '1'),
                ('coverage_end', str(target)),
            ],
        )
        db.execute(
            "INSERT INTO observations(kind,code,day,status) VALUES ('shares',?,?,'complete')",
            (code, str(target)),
        )
    held = api_cache.acquire()
    assert held is not None
    try:
        assert cli.main(['--module', 'etf-shares']) == 2
        assert json.loads(capsys.readouterr().out)['status'] == 'waiting_lock'
    finally:
        held.release()
    assert cli.main(['--module', 'etf-shares', '--full']) == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == 'SOURCE_DATA_INCOMPLETE'
    assert api_cache.read_state()['modules']['etf-shares']['file'] == initial
    with Session(source) as session:
        session.add(row(target, 12))
        session.commit()
    assert cli.main(['--module', 'etf-shares']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'published' and receipt['latest_share_date'] == str(target)
    assert not collector.status()['cache_pending']
    with api_cache.session('etf-shares') as local:
        assert local.get(ETFShare, (code, target)).shares == 12
    assert all(accesses)
    api_cache.source.dispose()
