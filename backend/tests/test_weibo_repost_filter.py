"""Offline fixtures: no real Weibo requests or account credentials."""
from unittest.mock import patch

import httpx
import pytest

from app.services.social_sources import fetch_posts, is_uncommented_weibo_repost


ORIGINAL = {'id': '10', 'user': {'screen_name': '科罗廖夫'},
            'text': '广州日报的评论，非常有力量。#绑匪冒充警察在停车场劫走徽剑#',
            'retweeted_status': {'id': '9', 'text': '更早的原帖'}}


@pytest.mark.parametrize('text', ['', '  \n\t', '转发微博', ' <p>转发微博</p> ',
                                  '<div>&nbsp;&#160;<br>\u3000</div>', '&#36716;发微博'])
def test_empty_outer_comment_is_filtered(text):
    assert is_uncommented_weibo_repost({'text': text, 'retweeted_status': ORIGINAL})


@pytest.mark.parametrize('text', ['赞', '👍', '[赞]', '转发微博！', '我想转发微博',
                                  'https://example.com', '<a href="https://example.com">链接</a>',
                                  '<a href="https://example.com"></a>', '<img alt="[赞]" src="x">',
                                  '<img src="x">', '转发微博//@甲:有道理',
                                  '转发 @科罗廖夫：原文', '<video src="x"></video>'])
def test_comment_expressions_and_uncertain_combined_text_are_kept(text):
    assert not is_uncommented_weibo_repost({'text': text, 'retweeted_status': ORIGINAL})


@pytest.mark.parametrize('item', [{'text': '转发微博'}, {'text': ''},
                                  {'text': '转发微博', 'retweeted_status': None},
                                  {'text': '转发微博', 'retweeted_status': {}},
                                  {'text': '转发微博', 'retweeted_status': 'unknown'},
                                  {'retweeted_status': ORIGINAL},
                                  {'text': None, 'retweeted_status': ORIGINAL},
                                  {'text': [], 'retweeted_status': ORIGINAL}])
def test_originals_and_unknown_structures_are_kept(item):
    assert not is_uncommented_weibo_repost(item)


def post(identifier, text='转发微博', repost=True, **extra):
    return dict(id=identifier, text=text, user={'id': 123},
                created_at='Sun Oct 25 12:00:00 +0800 2026',
                **({'retweeted_status': ORIGINAL} if repost else {}), **extra)


def run_pages(pages, source, budget=20):
    requests, checkpoints = [], []

    def handler(request):
        # Filtering must happen before long-text downloads.
        assert request.url.path == '/api/container/getIndex'
        page = int(request.url.params['page'])
        requests.append(page)
        return httpx.Response(200, json={'ok': 1, 'data': {
            'cards': [{'card_group': [{'mblog': item} for item in pages.get(page, [])]}]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts(dict(platform='weibo', uid='123', _initial=False,
                                  _on_page=lambda rows, next_page, done: checkpoints.append(
                                      ([row['id'] for row in rows], next_page, done)), **source), budget)
    return result, requests, checkpoints


def test_filtered_page_does_not_end_scan_or_download_long_text(caplog):
    with caplog.at_level('INFO'):
        result, requests, checkpoints = run_pages({
            1: [post('104', isLongText=True), post('103', '')],
            2: [post('102', '赞'), post('101', '转发微博', repost=False)],
        }, {})
    assert [row['id'] for row in result] == ['101', '102']
    assert result.complete and requests == [1, 2, 3]
    assert checkpoints == [([], 2, False), (['102', '101'], 3, False), ([], 1, True)]
    assert 'page=1 count=2' in caplog.text


def test_filtered_known_boundary_still_completes_scan():
    result, requests, checkpoints = run_pages({1: [post('102', '赞'), post('100')]},
                                              {'_known_ids': {'100'}})
    assert [row['id'] for row in result] == ['102']
    assert result.complete and requests == [1]
    assert checkpoints == [(['102'], 1, True)]


def test_filtered_resume_page_preserves_deeper_cursor():
    result, requests, checkpoints = run_pages({1: [post('105')], 4: [post('102')],
                                               5: [post('101', '赞')]}, {'_scan_page': 4}, 3)
    assert [row['id'] for row in result] == ['101']
    assert not result.complete and requests == [1, 4, 5]
    assert checkpoints == [([], 4, False), ([], 5, False), (['101'], 6, False)]


def test_filtered_timestamp_boundary_is_preserved():
    result, requests, checkpoints = run_pages({1: [post('100')]},
        {'_stop_before': '2026-10-25T13:00:00+08:00'})
    assert result == [] and result.complete and requests == [1]
    assert checkpoints == [([], 1, True)]


def test_xueqiu_repost_is_unaffected():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={'list': [dict(post('100'), created_at=1792900800000)]})))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts({'platform': 'xueqiu', 'uid': '123', 'cookie': 'fixture'}, 1)
    assert len(result) == 1


@pytest.mark.parametrize('field', ['description', 'title'])
def test_alternate_outer_content_is_kept(field):
    assert not is_uncommented_weibo_repost(dict(post('100', ''), **{field: '我赞同'}))


def test_expanded_long_text_rechecks_outer_comment():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith('/extend'):
            return httpx.Response(200, json={'ok': 1, 'data': {'longTextContent': '转发微博'}})
        return httpx.Response(200, json={'ok': 1, 'data': {'cards': [{'mblog': post(
            '100', '转发微博<a href="/detail/100">全文</a>', isLongText=True)}]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch('app.services.social_sources.httpx.Client', return_value=client):
        result = fetch_posts({'platform': 'weibo', 'uid': '123'}, 1)
    assert result == [] and result.complete
    assert requests == ['/api/container/getIndex', '/statuses/extend']
