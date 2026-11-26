from datetime import datetime, timezone

import pytest

from app.services.notification_time import notification_time
from app.services.social_sources import message_parts


@pytest.mark.parametrize(
    'value',
    [
        '2026-11-26T10:17:20+08:00',
        '2026-11-26T02:17:20Z',
        datetime(2026, 11, 26, 2, 17, 20, tzinfo=timezone.utc),
    ],
)
def test_notification_time_beijing(value):
    assert notification_time(value) == '2026-11-26 10:17:20'


@pytest.mark.parametrize('value', [None, '', '2026-11-26T10:17:20', 'invalid'])
def test_unknown_timestamp_is_not_invented(value):
    assert notification_time(value) == '未知'


def test_social_notification_formats_only_metadata():
    post = dict(
        name='博主',
        time='2026-11-26T02:17:20Z',
        text='原文 2026-11-26T02:17:20Z',
        url='https://example.com/post',
    )
    text = ''.join(message_parts('weibo', post))
    assert '时间：2026-11-26 10:17:20' in text
    assert post['text'] in text
