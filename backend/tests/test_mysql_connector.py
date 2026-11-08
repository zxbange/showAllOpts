"""Shared collector configuration and engine options; no real database access."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy

import mysql_connector as connector

ROOT = Path(__file__).resolve().parents[2]
CONFIG = dict(host='fixture.invalid', port=3307, dbname='fixture', dbuser='writer', dbpassword='fake/@ secret')


def test_configuration_copies_are_shared_but_not_mutable_aliases(monkeypatch):
    monkeypatch.setattr(connector, 'DB_CONFIG', CONFIG.copy())
    first, second = connector.get_database_config(), connector.get_database_config()
    first['host'] = 'changed.invalid'
    assert second == CONFIG and connector.DB_CONFIG == CONFIG
    options = connector.get_database_options()
    options['retry_count'] = 99
    assert connector.get_database_options()['retry_count'] != 99


def test_engine_uses_central_credentials_options_and_special_password(monkeypatch):
    monkeypatch.setattr(connector, 'DB_CONFIG', CONFIG.copy())
    options = dict(connector.DB_OPTIONS, driver='pymysql', connect_timeout=17, pool_recycle=7200)
    monkeypatch.setattr(connector, 'DB_OPTIONS', options)
    engine = SimpleNamespace(hide_parameters=False)
    calls = []
    def create(url, **kwargs):
        calls.append((url, kwargs))
        return engine
    monkeypatch.setattr(sqlalchemy, 'create_engine', create)
    assert connector.create_mysql_engine() is engine
    url, opts = calls[0]
    assert url.host == CONFIG['host'] and url.port == CONFIG['port']
    assert url.database == CONFIG['dbname'] and url.password == CONFIG['dbpassword']
    assert url.drivername == 'mysql+pymysql'
    assert opts['connect_args']['connect_timeout'] == 17 and opts['pool_recycle'] == 7200
    assert engine.hide_parameters is True


def test_bad_driver_does_not_echo_input():
    with pytest.raises(ValueError) as error:
        connector.mysql_drivername('fixture-secret')
    assert 'fixture-secret' not in str(error.value)


@pytest.mark.parametrize('key,value', [('retry_count', 0), ('connect_timeout', True), ('pool_size', -1)])
def test_invalid_options_fail_before_connecting(key, value):
    options = dict(connector.DB_OPTIONS, **{key: value})
    with pytest.raises(ValueError):
        connector.engine_options(options)


def test_exchange_cli_passes_shared_engine_factory(monkeypatch):
    spec = importlib.util.spec_from_file_location('offline_exchange_entry', ROOT/'collectors/etf_exchange_collector.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = []
    monkeypatch.setattr(module, '_exchange_main', lambda argv, engine_factory: seen.append((argv, engine_factory)) or 0)
    assert module.main(['--write']) == 0
    assert seen == [(['--write'], connector.create_mysql_engine)]


@pytest.mark.parametrize('write,fail', [(False, False), (True, False), (True, True)])
def test_exchange_shared_engine_is_only_opened_for_writes_and_always_disposed(monkeypatch, write, fail):
    from contextlib import nullcontext
    from datetime import date
    from app.services import etf_exchange as service

    day = date(2026, 11, 6)
    calendar = SimpleNamespace(days=[day], default_range=lambda _: (day, day))
    monkeypatch.setattr(service.httpx, 'Client', lambda **_: nullcontext(None))
    monkeypatch.setattr(service, 'ExchangeClient', lambda _: SimpleNamespace(calendar=lambda: calendar))
    monkeypatch.setattr(service, 'collect', lambda *a, **k: ([], [], {}))
    events = []
    engine = SimpleNamespace(dispose=lambda: events.append('disposed'))
    def factory():
        events.append('opened')
        return engine
    def save(actual, shares, prices, **kwargs):
        assert actual is engine and kwargs['calendar'] is calendar
        events.append('written')
        if fail:
            raise RuntimeError('fixture failure')
    monkeypatch.setattr(service, 'write_records', save)
    assert service.main(['--write'] if write else [], engine_factory=factory) == (1 if fail else 0)
    assert events == (['opened', 'written', 'disposed'] if write else [])
