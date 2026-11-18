"""Social status lights observe scans, not notification delivery."""
import sys
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from app.core.config import Settings
from app.services.social import SocialMonitor
from app.services.social_sources import PostBatch, MonitoringDeferred
from test_social import clock

@pytest.fixture
def scan(tmp_path, monkeypatch):
    events = []
    monkeypatch.setitem(sys.modules, 'app.services.collection_outcomes',
                        SimpleNamespace(record_outcome=lambda settings, key, **kw: events.append((key, kw))))
    with clock('2026-10-26T12:00:00+08:00'):
        service = SocialMonitor(Settings(_env_file=None, log_dir=tmp_path, social_worker_enabled=False))
        monkeypatch.setattr(service, 'send_alerts', lambda: None)
        monkeypatch.setattr(service, 'deliver_notifications', lambda config: None)
        yield service, events

@pytest.mark.parametrize('platform,key', [('weibo', 'social-weibo'), ('xueqiu', 'social-xueqiu')])
@pytest.mark.parametrize('complete,expected', [(True, 'success'), (False, 'partial')])
def test_author_scan_completion_is_not_page_success(scan, platform, key, complete, expected):
    service, events = scan
    service.add_author(platform, '123456', 'Test')
    service.update_config({'token': 'test', 'chat_id': '123', 'enabled': True})
    with patch('app.services.social.fetch_posts', return_value=PostBatch([], complete=complete)):
        service.cycle()
    assert [(key, event['outcome']) for key, event in events] == [(key, expected)]
    assert events[0][1]['trigger'] == 'scheduled'

def test_failure_is_recorded_and_later_success_does_not_mutate_event(scan):
    service, events = scan
    service.add_author('weibo', '123456', 'Test')
    service.update_config({'token': 'test', 'chat_id': '123', 'enabled': True})
    with patch('app.services.social.fetch_posts', side_effect=RuntimeError('test failure')):
        service.cycle()
    service.platform_state('weibo', retry_at=0)
    with clock('2026-10-26T13:00:00+08:00'), patch('app.services.social.fetch_posts', return_value=PostBatch([], complete=True)):
        service.cycle()
    assert [event['outcome'] for _, event in events] == ['failed', 'success']
    assert events[0][1]['run_key'] != events[1][1]['run_key']

def test_delivery_failure_does_not_turn_successful_scan_red(scan, monkeypatch):
    service, events = scan
    service.add_author('weibo', '123456', 'Test')
    service.update_config({'token': 'test', 'chat_id': '123', 'enabled': True})
    def fail(config): raise RuntimeError('delivery failed')
    monkeypatch.setattr(service, 'deliver_notifications', fail)
    with patch('app.services.social.fetch_posts', return_value=PostBatch([], complete=True)):
        service.cycle()
    assert events[0][1]['outcome'] == 'success'

def test_deferred_scan_is_not_success(scan):
    service, events = scan
    service.add_author('weibo', '123456', 'Test')
    service.update_config({'token': 'test', 'chat_id': '123', 'enabled': True})
    with patch('app.services.social.fetch_posts', side_effect=MonitoringDeferred('paused')):
        service.cycle()
    assert events[0][1]['outcome'] == 'cancelled'

@pytest.mark.parametrize('complete,expected', [(True, 'success'), (False, 'partial')])
def test_cube_scan_completion(scan, complete, expected):
    service, events = scan
    service.add_cube('ZH123456', 'Test')
    service.update_config({'token': 'test', 'chat_id': '123', 'enabled': True})
    with patch('app.services.social.fetch_cube_rebalances', return_value=PostBatch([], complete=complete)):
        service.cycle()
    assert [(key, event['outcome']) for key, event in events] == [('social-cubes', expected)]

def test_new_failure_is_not_masked_by_author_previous_success(scan):
    service, events = scan
    service.add_author('weibo', '123456', 'Test')
    service.update_config({'token': 'test', 'chat_id': '123', 'enabled': True})
    with patch('app.services.social.fetch_posts', return_value=PostBatch([], complete=True)):
        service.cycle()
    with clock('2026-10-26T13:00:00+08:00'), patch('app.services.social.fetch_posts', side_effect=RuntimeError('failed')):
        service.cycle()
    assert [event['outcome'] for _, event in events] == ['success', 'failed']


def test_no_scan_does_not_invent_success(scan):
    service, events = scan
    service.cycle()
    assert events == []
