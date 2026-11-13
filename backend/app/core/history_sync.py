"""Service-owned history adapters for the existing local-data sync pipeline.

The page stores remain authoritative. Under the collector's cross-process lock,
validate/import into a copy, then publish it atomically. Full reconciliation never
truncates public-source history or overwrites a previously published value.
"""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid

from app.core.cache_errors import SnapshotDataError
from app.core.sync_progress import report

MODULES = {'growth-dividend': ('创红比', ()), 'valuation-ruler': ('估值标尺', ())}
OWNER_FILE = '.database-sync-owner'
SYNC_MESSAGE = '数据库历史正在同步，请到数据同步中心查看或停止。'


def clear_sync_owner(service):
    """Called only while holding collection.lock, including stale-lock recovery."""
    (service.root / OWNER_FILE).unlink(missing_ok=True)


def mark_sync_owner(service):
    """Record ownership before acquire(for_history=True) returns to its caller."""
    (service.root / OWNER_FILE).write_text('database-history', encoding='utf-8')


def database_syncing(service, busy):
    return bool(busy and (service.root / OWNER_FILE).is_file())


def source_identity(source):
    url = source.url
    identity = [url.drivername, url.host, url.port, url.database, url.username]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def snapshot_matches(path, identity):
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT identity FROM history_cache_source_v1 WHERE id=1').fetchone()
            return row is not None and row[0] == identity
    except sqlite3.Error:
        return False


def service_for(cache, module):
    services = cache.history_services
    if module not in services:
        # Also support the existing refresh_cache CLI; no separate engine/config.
        if module == 'growth-dividend':
            from app.services.growth_dividend import GrowthDividend
            services[module] = GrowthDividend(cache.settings)
        else:
            from app.services.valuation_ruler import ValuationRuler
            services[module] = ValuationRuler(cache.settings)
    return services[module]


def copy_history(cache, module, full=False):
    from app.core.cache import SyncCancelled, now
    service = service_for(cache, module)
    lock = service.acquire(for_history=True)
    if lock is None:
        raise SnapshotDataError('该模块正在采集公开行情，请等待采集完成后从数据同步中心重试。')
    filename = module + '-' + uuid.uuid4().hex + '.sqlite'
    snapshot = cache.root / filename
    started = time.monotonic()
    source = cache.source
    identity = source_identity(source)
    stage = ['校验完整源历史' if full else '核对源历史并补齐缺失记录']

    def check():
        if cache.stop_event.is_set() or service.stop_event.is_set():
            raise SyncCancelled()
        if time.monotonic() - started > 1800:
            raise SnapshotDataError('历史同步超过安全时间上限，请稍后重试。')
        # The existing cache reporter owns run/module cancellation and progress.
        report(stage[0])

    try:
        check()
        # Same filesystem as the live DB, even when CACHE_DIR is on another mount.
        with tempfile.TemporaryDirectory(prefix='.cache-sync-', dir=service.root) as directory:
            staged = copy.copy(service)
            staged.root = Path(directory)
            path = staged.root / 'history.sqlite3'
            with service.db() as existing, closing(sqlite3.connect(path)) as target:
                existing.backup(target, pages=256, progress=lambda *_: check())
            public_state = staged.read_state()
            state = copy.deepcopy(public_state)
            moment = now()
            if module == 'growth-dividend':
                from app.services import growth_dividend_import as importer
                from app.services.growth_dividend import close_cutoff
                days, points = importer.read_source(source, close_cutoff(moment), check)
                stage[0] = '追加缺失指数历史，保留已有值'
                report(stage[0], percent=65)
                summary = importer.publish(staged, days, points, check)
                missing = sum(item['missing'] for item in summary['indices'].values())
                note = '新增 {} 条；冲突 {} 条未覆盖；源日历缺失 {} 条。'.format(
                    summary['inserted'], summary['conflicts'], missing)
                summary['imported_at'] = moment.isoformat()
                sync_metadata = dict(sync_summary=summary)
            else:
                from app.services import valuation_ruler_import as importer
                from app.services.valuation_ruler import close_cutoff
                receipts = (importer.read_receipts(staged) if not full and snapshot_matches(path, identity) else None)
                def valuation_progress(message, percent):
                    stage[0] = message
                    report(message, percent=percent)
                with importer.read_source(source, close_cutoff(moment), check, stage_dir=staged.root,
                                          receipts=receipts, progress=valuation_progress) as data:
                    stage[0] = '追加缺失估值历史，保留已有值'
                    report(stage[0], percent=65)
                    importer.publish(staged, state, moment, data, check)
                sync_metadata = {key: state[key] for key in (
                    'joinquant_import_at', 'joinquant_imported', 'joinquant_conflicts', 'joinquant_missing_days',
                    'joinquant_skipped_days')}
                sync_metadata['joinquant_import_warning'] = state.get('error')
                note = '新增 {} 天；跳过未变化 {} 天；冲突 {} 天未覆盖；源日历缺失 {} 天。'.format(
                    state['imported_days'], state['joinquant_skipped_days'], state['conflict_days'], state['joinquant_missing_days'])
            # A validated database import is not a successful public collection,
            # nor proof of complete historical coverage. Preserve all public run
            # state, including its previous success timestamp, errors and phase.
            state = {**public_state, **sync_metadata}
            with staged.db() as db:
                # The copy is private and the live collector lock is held. Write
                # without write_state's public-collection updated_at side effect.
                db.execute('UPDATE state SET payload=? WHERE id=1',
                           (json.dumps(state, ensure_ascii=False),))
                db.execute('CREATE TABLE IF NOT EXISTS history_cache_source_v1 '
                           '(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)')
                db.execute('INSERT INTO history_cache_source_v1 VALUES (1,?) '
                           'ON CONFLICT(id) DO UPDATE SET identity=excluded.identity', (identity,))
                rows = db.execute('SELECT COUNT(*) FROM points').fetchone()[0]
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise SnapshotDataError('历史快照校验失败，已有数据未修改。')
            stage[0] = '发布已校验的本地历史'
            report(stage[0], percent=95)
            # Keep an immutable cache snapshot for restart/status, without making
            # page APIs read a different database from their public collectors.
            shutil.copyfile(path, snapshot)
            # Windows fsync requires a writable handle; r+b preserves the copied bytes.
            with snapshot.open('r+b') as stream:
                os.fsync(stream.fileno())
            check()
            if source_identity(cache.source) != identity:
                raise SnapshotDataError('数据库来源已变更，请重新同步。')
            os.replace(path, service.root / 'history.sqlite3')
            # This is the commit boundary. Later cancellation must not mislabel an
            # already published import as cancelled or discard its cache receipt.
            cache.sync_notes[module] = note
            return filename, rows
    except BaseException:
        snapshot.unlink(missing_ok=True)
        raise
    finally:
        # Retain ownership until AFTER unlocking: the next successful public
        # acquisition/status probe clears it. Never expose an old public run_id
        # in an unlink-before-unlock window. A crash leaves the same safe marker.
        lock.release()
