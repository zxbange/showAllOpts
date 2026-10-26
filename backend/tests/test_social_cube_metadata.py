"""Portfolio labels are fetched in the scheduled queue, cached, and migrated safely."""
import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.social_cube_sources import cube_message_parts, fetch_cube_rebalances
from app.services.social_cubes import CubeMonitorMixin
from app.services.social_sources import MonitoringDeferred, PlatformIntervention, PostBatch, RateLimited
from test_social_cubes import add_cube, clock, enable, event, next_tick


@contextmanager
def transport(handler):
    original = httpx.Client
    with patch('app.services.social_cube_sources.httpx.Client',
               side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
        yield


@pytest.fixture
def metadata_client(engine, tmp_path):
    with clock('2026-10-26T12:00:00+08:00'):
        settings = Settings(_env_file=None, log_dir=tmp_path, cache_enabled=False, social_worker_enabled=False)
        app = create_app(settings, engine)
        with TestClient(app) as client, \
                patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([])), \
                patch('app.services.social.random.uniform', return_value=780), \
                patch.object(app.state.social, 'send_part') as send, \
                patch.object(app.state.social.stop_event, 'wait', return_value=False):
            yield client, app.state.social, send


def test_source_resolves_names_with_same_cookie_and_guarded_request_spacing():
    requested, waits, metadata = [], [], []

    def handler(request):
        requested.append(request.url.path)
        assert request.url.host == 'www.xueqiu.com'
        assert request.headers['Cookie'] == 'private-test-cookie'
        if request.url.path == '/cubes/show.json':
            assert dict(request.url.params) == {'symbol': 'ZH1'}
            return httpx.Response(200, json={'symbol': 'ZH1', 'name': '<b>真实组合</b>', 'owner_id': 123,
                                             'description': '请登录和验证码只是正常的组合说明'})
        if request.url.path == '/user/show.json':
            assert dict(request.url.params) == {'id': '123'}
            return httpx.Response(200, json={'id': 123, 'screen_name': '真实主理人',
                                             'description': '请登录和验证码只是正常的个人简介'})
        assert request.url.path == '/cubes/rebalancing/history.json'
        return httpx.Response(200, json={'list': []})

    with transport(handler):
        fetch_cube_rebalances({'symbol': 'ZH1', 'cookie': 'private-test-cookie',
                               '_metadata_lookup_due': True, '_on_metadata': metadata.append,
                               '_wait': lambda seconds: waits.append(seconds) or False})
    assert requested == ['/cubes/show.json', '/user/show.json', '/cubes/rebalancing/history.json']
    assert len(waits) == 2 and all(1 <= delay <= 3 for delay in waits)
    assert metadata == [{'cube_name': '真实组合', 'owner_name': '真实主理人', 'owner_id': '123'}]


def test_embedded_owner_avoids_extra_profile_request():
    requested, metadata = [], []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == '/cubes/show.json':
            return httpx.Response(200, json={'symbol': 'ZH1', 'name': '真实组合', 'owner_id': 123,
                                             'owner': {'id': 123, 'screen_name': '主理人'}})
        return httpx.Response(200, json={'list': []})

    with transport(handler):
        fetch_cube_rebalances({'symbol': 'ZH1', '_metadata_lookup_due': True,
                               '_on_metadata': metadata.append})
    assert requested == ['/cubes/show.json', '/cubes/rebalancing/history.json']
    assert metadata[0]['owner_name'] == '主理人'


@pytest.mark.parametrize('bad_payload', [
    {'symbol': 'ZH2', 'name': '别的组合', 'owner_id': 123},
    {'symbol': 'ZH1', 'name': ''},
    [],
])
def test_invalid_metadata_does_not_prevent_history_or_save_wrong_identity(bad_payload):
    requested, metadata = [], []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, json=bad_payload if request.url.path == '/cubes/show.json' else {'list': []})

    with transport(handler):
        fetch_cube_rebalances({'symbol': 'ZH1', '_metadata_lookup_due': True,
                               '_on_metadata': metadata.append})
    assert metadata == [{}]
    assert requested == ['/cubes/show.json', '/cubes/rebalancing/history.json']


def test_profile_failure_keeps_valid_portfolio_name_and_continues_history():
    metadata = []

    def handler(request):
        if request.url.path == '/cubes/show.json':
            return httpx.Response(200, json={'symbol': 'ZH1', 'name': '真实组合', 'owner_id': 123})
        if request.url.path == '/user/show.json':
            return httpx.Response(500, json={})
        return httpx.Response(200, json={'list': []})

    with transport(handler):
        fetch_cube_rebalances({'symbol': 'ZH1', '_metadata_lookup_due': True,
                               '_on_metadata': metadata.append})
    assert metadata == [{'cube_name': '真实组合', 'owner_id': '123', 'owner_name': ''}]


@pytest.mark.parametrize('status,error', [(403, PlatformIntervention), (429, RateLimited)])
def test_restriction_during_metadata_stops_history_without_retry(status, error):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(status, json={})

    with transport(handler), pytest.raises(error):
        fetch_cube_rebalances({'symbol': 'ZH1', '_metadata_lookup_due': True})
    assert requested == ['/cubes/show.json']


def test_metadata_cannot_make_requests_when_queue_guard_stops():
    requested = []
    with transport(lambda request: requested.append(request) or httpx.Response(200, json={})), \
            pytest.raises(MonitoringDeferred):
        fetch_cube_rebalances({'symbol': 'ZH1', '_metadata_lookup_due': True, '_should_stop': lambda: True})
    assert requested == []


def test_existing_cube_backfills_names_without_losing_alias_and_uses_daily_cache(metadata_client):
    client, service, _ = metadata_client
    original = add_cube(service, name='我的备注')
    enable(service)
    requested = []

    def fetch(source, pages):
        requested.append(source['_metadata_lookup_due'])
        if source['_metadata_lookup_due']:
            source['_on_metadata']({'cube_name': '真实组合', 'owner_name': '主理人', 'owner_id': '123'})
        return PostBatch([event('100', time='2026-10-25T12:00:00+08:00')])

    with patch('app.services.social.fetch_cube_rebalances', side_effect=fetch):
        service.cycle()
        with next_tick(service):
            service.cycle()
    cube = service.overview()['cubes'][0]
    assert cube['id'] == original['id'] and cube['name'] == '我的备注'
    assert cube['cube_name'] == '真实组合' and cube['owner_name'] == '主理人'
    assert cube['owner_id'] == '123'
    assert requested == [True, False]
    history = client.get('/api/v1/social-monitor/cube-rebalances').json()['items'][0]
    assert history['cube_name'] == '真实组合' and history['cube_alias'] == '我的备注'
    assert history['owner_name'] == '主理人'
    with clock('2026-10-27T12:01:00+08:00'), patch('app.services.social.fetch_cube_rebalances', side_effect=fetch):
        service.cycle()
    assert requested[-1] is True


def test_failed_metadata_attempt_is_throttled_and_preserves_cached_names(metadata_client):
    _, service, _ = metadata_client
    cube = add_cube(service)
    service.update_cube_metadata(cube, {'cube_name': '已知组合', 'owner_name': '旧主理人', 'owner_id': '123'})
    service.update_cube_metadata(cube, {})
    cached = service.overview()['cubes'][0]
    assert cached['cube_name'] == '已知组合' and cached['owner_name'] == '旧主理人'
    service.update_cube_metadata(cube, {'cube_name': '已知组合', 'owner_id': '456'})
    changed = service.overview()['cubes'][0]
    assert changed['owner_id'] == '456' and changed['owner_name'] == ''
    enable(service)
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([])) as fetch:
        service.cycle()
        assert fetch.call_args.args[0]['_metadata_lookup_due'] is False
        with clock('2026-10-26T13:01:00+08:00'):
            service.cycle()
        assert fetch.call_args.args[0]['_metadata_lookup_due'] is True


def test_metadata_callback_cannot_update_deleted_or_outside_window_cube(metadata_client):
    _, service, _ = metadata_client
    cube = add_cube(service)
    with clock('2026-10-26T15:00:00+08:00'), pytest.raises(MonitoringDeferred):
        service.update_cube_metadata(cube, {'cube_name': '不应保存'})
    service.change_cube(cube['id'], delete=True)
    replacement = add_cube(service, name='新备注')
    with pytest.raises(MonitoringDeferred):
        service.update_cube_metadata(cube, {'cube_name': '不应保存'})
    assert service.overview()['cubes'][0]['id'] == replacement['id']
    assert service.overview()['cubes'][0]['cube_name'] == ''


def test_old_cube_schema_is_upgraded_without_changing_manual_alias():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE cubes(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT UNIQUE,name TEXT,enabled INTEGER)')
    db.execute("INSERT INTO cubes(symbol,name,enabled) VALUES('ZH1','历史备注',1)")
    CubeMonitorMixin.init_cube_schema(db)
    row = db.execute('SELECT symbol,name,cube_name,owner_name,owner_id,metadata_checked_at FROM cubes').fetchone()
    assert row == ('ZH1', '历史备注', '', '', '', None)
    db.close()


def test_telegram_message_contains_actual_name_owner_and_optional_alias():
    text = ''.join(cube_message_parts({'symbol': 'ZH1', 'name': '我的备注',
                                       'cube_name': '真实组合', 'owner_name': '主理人'}, event()))
    assert text.startswith('【雪球组合调仓】真实组合 ZH1\n主理人：主理人\n备注：我的备注\n')
