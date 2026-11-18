"""Local terminal-result observations with optional reversible active-history retention.

Never substitute the last success for a newer failure. Starting/running/skipped jobs
are not terminal successes and do not change the last completed result.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import json

from app.services.collection_errors import sanitize_error
import sqlite3

LOG = logging.getLogger(__name__)


class OutcomeUnavailable(RuntimeError):
    pass

MODULES = ('etf-shares', 'market-sentiment', 'market-crowding', 'growth-dividend',
           'margin-guarantee', 'valuation-ruler', 'commodities-a00', 'commodities-cci',
           'cash-cow', 'cash-cow-3', 'cash-cow-5', 'cash-cow-10', 'dividend-selection',
           'social-weibo', 'social-xueqiu', 'social-cubes', 'industry-momentum',
           'microcap-temperature', 'microcap-breadth', 'etf-momentum', 'institution-ownership', 'personal-holdings')
SYNC_MODULES = frozenset(('industry-momentum', 'microcap-temperature', 'microcap-breadth',
                         'etf-momentum', 'institution-ownership', 'personal-holdings'))
ALIASES = {'complete': 'success', 'completed': 'success', 'error': 'failed',
           'publication_pending': 'partial'}
TERMINAL = frozenset(('success', 'failed', 'partial', 'cancelled', 'interrupted', 'unknown'))


@contextmanager
def database(settings):
    root = settings.log_path / 'collection-outcomes'
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / 'outcomes.sqlite3', timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS results '
                           '(module TEXT NOT NULL, run_key TEXT NOT NULL, outcome TEXT NOT NULL, '
                           'finished_at TEXT NOT NULL, trigger TEXT NOT NULL, PRIMARY KEY(module,run_key))')
        connection.execute('CREATE INDEX IF NOT EXISTS latest_results ON results(module,finished_at DESC)')
        connection.execute('CREATE TABLE IF NOT EXISTS result_details '
                           '(module TEXT NOT NULL, run_key TEXT NOT NULL, payload TEXT NOT NULL, '
                           'PRIMARY KEY(module,run_key))')
        with connection:
            yield connection
    finally:
        connection.close()


def record_outcome(settings, module_key, *, run_key, outcome, trigger, finished_at=None, error_details=None):
    """Best-effort observation only: cannot turn a committed collection into failure."""
    outcome = ALIASES.get(outcome, outcome)
    if module_key not in MODULES or outcome not in TERMINAL or trigger not in ('manual', 'scheduled'):
        return False
    try:
        stamp = datetime.fromisoformat(finished_at) if finished_at else datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            return False  # Unknown clock basis cannot establish most-recent completion.
        stamp = stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
        with database(settings) as db:
            db.execute('BEGIN IMMEDIATE')
            has_tombstones = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_tombstones'").fetchone()
            if has_tombstones and db.execute('SELECT 1 FROM result_tombstones WHERE module=? AND run_key=?',
                                             (module_key, str(run_key))).fetchone():
                return False
            has_archive = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='results_archive'").fetchone()
            if has_archive and db.execute('SELECT 1 FROM results_archive WHERE module=? AND run_key=?',
                                          (module_key, str(run_key))).fetchone():
                return False  # Archived completion still owns its idempotency key.
            inserted = bool(db.execute('INSERT OR IGNORE INTO results VALUES(?,?,?,?,?)',
                                   (module_key, str(run_key), outcome, stamp, trigger)).rowcount)
            details = sanitize_error(error_details) if outcome != 'success' else None
            if inserted and details:
                db.execute('INSERT INTO result_details VALUES(?,?,?)',
                           (module_key, str(run_key), json.dumps(details, ensure_ascii=False)))
        if inserted and getattr(settings, 'collection_history_retention_enabled', False):
            from app.services.collection_retention import retain_history
            try:
                retain_history(settings, apply=True, confirmed=True)
            except Exception as retention_error:
                LOG.warning('Collection history retention unavailable error_type=%s', type(retention_error).__name__)
        return inserted
    except Exception as exc:
        LOG.warning('Collection outcome recording unavailable module=%s error_type=%s', module_key, type(exc).__name__)
        return False


def outcome_history(settings, module_key, limit=5):
    """Bounded, read-only real completion history, never synthesized from latest state."""
    if module_key not in MODULES or type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError('Invalid collection history request')
    path = settings.log_path / 'collection-outcomes' / 'outcomes.sqlite3'
    if not path.exists():
        return {'items': []}
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                'SELECT run_key,outcome,finished_at,trigger FROM results WHERE module=? '
                'ORDER BY finished_at DESC,rowid DESC LIMIT ?', (module_key, limit)).fetchall()
            has_details = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_details'").fetchone()
            details = {}
            if has_details:
                for row in rows:
                    saved = connection.execute('SELECT payload FROM result_details WHERE module=? AND run_key=?',
                                               (module_key, row['run_key'])).fetchone()
                    if saved:
                        try:
                            details[row['run_key']] = sanitize_error(json.loads(saved[0]))
                        except (ValueError, TypeError):
                            pass
        finally:
            connection.close()
        kind = 'database_sync' if module_key in SYNC_MODULES else 'collection'
        return {'items': [dict(row) | {'kind': kind, 'error_details': details.get(row['run_key'])} for row in rows]}
    except (sqlite3.Error, OSError, ValueError) as exc:
        LOG.warning('Collection history reading unavailable module=%s error_type=%s',
                    module_key, type(exc).__name__)
        raise OutcomeUnavailable('采集历史暂不可用') from None


def latest_outcomes(settings):
    items = {key: dict(outcome='unknown', finished_at=None, trigger=None,
                       kind='database_sync' if key in SYNC_MODULES else 'collection') for key in MODULES}
    unresolved = {}
    for key, value in legacy_outcomes(settings).items():
        value = dict(value)
        if '_unresolved_run' in value:
            unresolved[key] = value.pop('_unresolved_run')
        items[key].update(value)
    if not (settings.log_path / 'collection-outcomes' / 'outcomes.sqlite3').exists():
        return {'items': items}
    try:
        with database(settings) as db:
            rows = [db.execute('SELECT module,run_key,outcome,finished_at,trigger FROM results WHERE module=? '
                               'ORDER BY finished_at DESC,rowid DESC LIMIT 1', (key,)).fetchone() for key in MODULES]
        for row in rows:
            if row is not None and row['module'] in items:
                if row['module'] in unresolved and (not unresolved[row['module']] or row['run_key'] != unresolved[row['module']]):
                    continue
                current_stamp = items[row['module']]['finished_at']
                if current_stamp and datetime.fromisoformat(current_stamp) > datetime.fromisoformat(row['finished_at']):
                    continue
                items[row['module']].update({key: row[key] for key in ('outcome', 'finished_at', 'trigger')})
    except Exception as exc:
        LOG.warning('Collection outcome reading unavailable error_type=%s', type(exc).__name__)
        raise OutcomeUnavailable('采集结果暂不可用') from None
    return {'items': items}

TASK_MODULES = {
    'commodities': ('commodities-a00',), 'cci': ('commodities-cci',),
    'margin': ('margin-guarantee',), 'growth_dividend': ('growth-dividend',),
    'valuation_ruler': ('valuation-ruler',), 'dividend_selection': ('dividend-selection',),
    'cash_cow': ('cash-cow', 'cash-cow-3', 'cash-cow-5', 'cash-cow-10'),
}


def record_task(settings, task, *, outcome, scheduled, run_key=None, error_details=None):
    from uuid import uuid4
    run_key = run_key or uuid4().hex
    for module in TASK_MODULES.get(task, ()):
        record_outcome(settings, module, run_key=run_key, outcome=outcome,
                       trigger='scheduled' if scheduled else 'manual', error_details=error_details)


def legacy_outcomes(settings):
    """Read only dated, explicit terminal evidence. Never infer from saved data."""
    import json
    found = {}
    sources = [('commodities', 'commodities', 'history.sqlite3'), ('cci', 'cci-coal', 'history.sqlite3'),
               ('cash_cow', 'cash-cow', 'screen.sqlite3'), ('growth_dividend', 'growth-dividend', 'history.sqlite3'),
               ('margin', 'margin-guarantee', 'history.sqlite3'), ('valuation_ruler', 'valuation-ruler', 'history.sqlite3'),
               ('dividend_selection', 'dividend-collection', 'state.sqlite3')]
    for task, directory, filename in sources:
        path = settings.log_path / 'market-data' / directory / filename
        if not path.exists():
            continue
        try:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            finally:
                db.close()
            state = json.loads(row[0]) if row else {}
            if not isinstance(state, dict):
                continue
            if state.get('active'):
                continue
            outcome = ALIASES.get(state.get('outcome'), state.get('outcome'))
            stamp = state.get('checkedAt') if task in ('commodities', 'cci') else None
            success = (state.get('phase') == 'complete' or state.get('stage') == 'complete' or
                       state.get('stage') in ('3/5/10年筛选完成', '更新完成', '没有新增披露') or
                       str(state.get('stage', '')).startswith('已更新至 '))
            if not stamp and success and not state.get('error'):
                outcome, stamp = 'success', state.get('last_success_at')
            if outcome not in TERMINAL or not stamp:
                if state.get('error') or state.get('phase') in ('failed', 'cancelled', 'interrupted') or state.get('stage') in ('failed', 'publication_pending'):
                    for module in TASK_MODULES[task]:
                        found[module] = dict(outcome='unknown', finished_at=None, trigger=None, _unresolved_run=state.get('run_id'))
                continue
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                continue
            attempt = state.get('last_attempt_at') or state.get('startedAt')
            if attempt and datetime.fromisoformat(attempt) > parsed:
                continue
            trigger = state.get('trigger')
            if trigger not in ('manual', 'scheduled'):
                trigger = 'scheduled' if state.get('current_slot') or state.get('current_scheduled_slot') else None
            for module in TASK_MODULES[task]:
                found[module] = dict(outcome=outcome, finished_at=parsed.astimezone(timezone.utc).isoformat(), trigger=trigger)
        except (ValueError, TypeError, sqlite3.Error, OSError):
            continue
    return found
