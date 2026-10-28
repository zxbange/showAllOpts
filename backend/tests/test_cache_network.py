"""Bounded MySQL reads and atomic retry of interrupted snapshot transfers."""
from datetime import date, datetime
import importlib.util
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError
from pymysql.err import OperationalError as MysqlError
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.core import cache_sync, database
from app.core.cache import SnapshotCache
from app.core.config import Settings
from app.models.momentum import IndustryMomentum
from test_cache import cached
from test_cache_incremental import assert_matches
from test_momentum_api import row


def lost_connection(code=2013):
    return OperationalError('SELECT secret_sql', {}, MysqlError(code, 'secret_network_detail'))


def test_reads_use_bounded_buffered_pages_across_composite_key_boundaries(cached, session, engine, monkeypatch):
    _, cache = cached
    monkeypatch.setattr(cache_sync, 'READ_PAGE_SIZE', 2)
    session.add_all([
        row(date(2026,1,5), 'B', 1), row(date(2026,1,5), 'A', 2, kind='sw_l1'),
        row(date(2026,1,7), 'C', 3), row(date(2026,1,7), 'D', 4),
    ])
    session.commit()
    statements=[]
    def capture(conn, cursor, sql, params, context, many):
        if 'GROUP BY' in sql:
            statements.append(sql)
            return
        assert context.execution_options.get('stream_results') is False
        assert 'ORDER BY' in sql and 'LIMIT' in sql
        assert params[-2:] == (2, 0)  # Each SQLite test query uses LIMIT 2, OFFSET 0; seek advances it.
        statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        cache.refresh('industry-momentum', background=False)
        assert len(statements)==5  # Bounds + 1 sw_l1 page + 3 sw_l2 pages.
        assert 'GROUP BY' in statements[0] and all('WHERE' in sql for sql in statements[1:])
        statements.clear()
        cache.refresh('industry-momentum', background=False)
        assert len(statements)==1
        assert 'GROUP BY' in statements[0]
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert_matches(cache, session, 'industry-momentum')


def test_interrupted_second_data_page_retries_cleanly_without_publishing_partial_copy(cached, session, engine, monkeypatch):
    _, cache = cached
    cache.refresh('industry-momentum', background=False)
    previous = cache.read_state()['modules']['industry-momentum']['file']
    monkeypatch.setattr(cache_sync, 'READ_PAGE_SIZE', 2)
    session.add_all([row(date(2026,1,7), code, 9) for code in ('B','C','D','E')])
    session.commit()
    calls=[]
    waits=[]
    def fail_once(conn, cursor, sql, *args):
        if 'industry_momentum.momentum,' in sql:
            calls.append(sql)
            if len(calls)==2:
                raise lost_connection()
    def wait(delay):
        assert cache.read_state()['modules']['industry-momentum']['file']==previous
        assert len(list(cache.root.glob('industry-momentum-*.sqlite')))==1
        waits.append(delay)
        return False
    monkeypatch.setattr(cache.stop_event, 'wait', wait)
    event.listen(engine, 'before_cursor_execute', fail_once)
    try:
        with cache.session('industry-momentum') as old:
            assert len(old.scalars(select(IndustryMomentum)).all())==2
            cache.refresh('industry-momentum', background=False)
            assert len(old.scalars(select(IndustryMomentum)).all())==2
    finally:
        event.remove(engine, 'before_cursor_execute', fail_once)
    assert waits==[2] and len(calls)==5
    assert len(list(cache.root.glob('industry-momentum-*.sqlite')))==2
    assert_matches(cache, session, 'industry-momentum')


def test_retry_exhaustion_keeps_old_cache_and_other_modules_continue(cached, monkeypatch):
    _, cache = cached
    cache.refresh(background=False)
    previous=cache.read_state()['modules']['industry-momentum'].copy()
    original=cache.copy_module
    calls=[]
    waits=[]
    def fail(module):
        calls.append(module)
        if module=='industry-momentum': raise lost_connection()
        return original(module)
    monkeypatch.setattr(cache, 'copy_module', fail)
    monkeypatch.setattr(cache.stop_event, 'wait', lambda delay: waits.append(delay) or False)
    cache.refresh(background=False)
    current=cache.read_state()['modules']['industry-momentum']
    assert current['file']==previous['file'] and current['updated_at']==previous['updated_at']
    assert 'MYSQL_2013' in current['error'] and 'secret' not in current['error']
    assert waits==[2,4] and calls.count('industry-momentum')==3
    assert all(m['ready'] and not m['error'] for m in cache.status()['modules'][1:])
    assert not cache.refreshing()


@pytest.mark.parametrize('error',[lost_connection(1045), sqlite3.OperationalError('disk full'), PermissionError('denied')])
def test_permanent_errors_are_not_retried(cached, monkeypatch, error):
    _, cache=cached
    calls=[]
    def fail(module):
        calls.append(module)
        raise error
    def forbidden_wait(delay):
        pytest.fail('Permanent failures must not be retried')
    monkeypatch.setattr(cache, 'copy_module', fail)
    monkeypatch.setattr(cache.stop_event, 'wait', forbidden_wait)
    cache.refresh('industry-momentum', background=False)
    assert calls==['industry-momentum']


def test_shutdown_interrupts_backoff_and_resumes_full_rebuild(cached, session, engine, monkeypatch):
    settings, cache=cached
    cache.refresh(background=False)
    previous=cache.read_state()['modules']['industry-momentum']['file']
    session.query(IndustryMomentum).update({'momentum':77})  # Force rebuild must preserve full=True on resume.
    session.commit()
    def fail(module, full=False):
        assert full is True
        raise lost_connection()
    def stop(delay):
        cache.stop_event.set()
        return True
    monkeypatch.setattr(cache, 'copy_module', fail)
    monkeypatch.setattr(cache.stop_event, 'wait', stop)
    cache.refresh('industry-momentum', background=False, full=True)
    state=cache.read_state()
    assert state['pending']==['industry-momentum'] and state['pending_full'] is True
    assert state['modules']['industry-momentum']['file']==previous
    assert not cache.refreshing()
    resumed=SnapshotCache(settings,engine)
    resumed.tick()
    assert resumed.read_state()['pending'] == ['industry-momentum']
    resumed.refresh('industry-momentum', background=False)
    assert_matches(resumed,session,'industry-momentum')


def test_background_source_timeout_is_separate_from_direct_page_queries(monkeypatch):
    options=[]
    monkeypatch.setattr(database,'create_engine',lambda url,**kwargs: options.append(kwargs))
    settings=Settings(_env_file=None,cache_db_read_timeout=150)
    database.build_engine(settings)
    database.build_engine(settings,for_cache=True)
    assert [o['connect_args']['read_timeout'] for o in options]==[20,150]
    assert all(o['connect_args']['connect_timeout']==8 and o['hide_parameters'] for o in options)


def test_app_and_cli_select_the_background_timeout(cached, engine, monkeypatch, capsys):
    import app.main as application
    settings,_=cached
    selected=[]
    def build(config, *, for_cache=False):
        selected.append(for_cache)
        return engine
    monkeypatch.setattr(application,'build_engine',build)
    application.create_app(settings)
    application.create_app(settings.model_copy(update={'cache_enabled':False}))
    path=Path(__file__).resolve().parents[2]/'scripts/refresh_cache.py'
    spec=importlib.util.spec_from_file_location('refresh_cache_timeout_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module,'build_engine',build)
    monkeypatch.setattr(module,'Settings',lambda:settings)
    monkeypatch.setattr(module,'configure_logging',lambda config:None)
    assert module.main(['--status'])==0
    assert selected==[True,False,True]


@pytest.mark.parametrize('values',[
    {'cache_db_read_timeout':0}, {'cache_db_read_timeout':3601},
    {'cache_sync_attempts':0}, {'cache_sync_attempts':6},
])
def test_background_sync_limits_are_validated(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None,**values)


@pytest.mark.parametrize('seconds', [None, 1800, 3600])
def test_large_sync_read_timeout_reaches_driver(monkeypatch, seconds):
    options = []
    monkeypatch.setattr(database, 'create_engine', lambda url, **kwargs: options.append(kwargs))
    monkeypatch.delenv('CACHE_DB_READ_TIMEOUT', raising=False)
    values = {} if seconds is None else {'cache_db_read_timeout': seconds}
    settings = Settings(_env_file=None, **values)
    database.build_engine(settings, for_cache=True)
    assert options[0]['connect_args']['read_timeout'] == (600 if seconds is None else seconds)
