from datetime import datetime
from unittest.mock import patch

import pytest
from app.core.config import Settings
from app.services.social import SocialMonitor
from test_social import clock


@pytest.mark.parametrize('stamp', ['2026-11-07T10:00:00+08:00', '2026-10-01T10:00:00+08:00', '2027-01-04T10:00:00+08:00'])
def test_calendar_blocks_cubes_not_blogger_posts(tmp_path, stamp):
    service = SocialMonitor(Settings(_env_file=None, log_dir=tmp_path, social_worker_enabled=False))
    service.add_author('weibo', '123456', 'test')
    service.update_config({'enabled': True, 'weibo_cookie': 'test-only', 'telegram_enabled': False})
    with clock(stamp), patch('app.services.social.fetch_posts', return_value=[]) as fetch:
        assert not service.night_time(datetime.fromisoformat(stamp))
        assert not service.cube_monitoring_time(datetime.fromisoformat(stamp))
        service.cycle()
        fetch.assert_called_once()
        with service.db() as db:
            assert service.config(db)['enabled']
    # Starting the monitor is not undone; a normal business session remains usable.
    with clock('2026-11-09T10:00:00+08:00'):
        assert not service.night_time(datetime.fromisoformat('2026-11-09T10:00:00+08:00'))


@pytest.mark.parametrize('stamp,expected', [
    ('2026-11-07T10:00:00+08:00', '2026-11-09T09:00:00+08:00'),
    ('2026-10-01T10:00:00+08:00', '2026-10-08T09:00:00+08:00'),
    ('2027-01-04T10:00:00+08:00', None),
])
def test_next_cube_check_skips_closed_calendar(tmp_path, stamp, expected):
    service = SocialMonitor(Settings(_env_file=None, log_dir=tmp_path, social_worker_enabled=False))
    actual = service.next_cube_check_time(datetime.fromisoformat(stamp))
    assert actual == (datetime.fromisoformat(expected).timestamp() if expected else None)


def test_unknown_calendar_cube_only_has_no_next_check(tmp_path):
    service = SocialMonitor(Settings(_env_file=None, log_dir=tmp_path, social_worker_enabled=False))
    service.add_cube('ZH123456')
    service.update_config({'enabled': True, 'xueqiu_cookie': 'test-only', 'telegram_enabled': False})
    with clock('2027-01-04T10:00:00+08:00'):
        service.save_next_check()
        with service.db() as db:
            assert service.config(db)['next_check'] is None
