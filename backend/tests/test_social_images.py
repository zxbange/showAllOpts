from unittest.mock import Mock, patch
import pytest
from test_social_channels import channels, clock, enable, post, config, deliveries
from app.services.social_images import image_url, extract_images, download_image, MAX_BYTES
from app.services.social_sources import normalize_post, is_uncommented_weibo_repost
URL = 'https://wx1.sinaimg.cn/large/test.jpg'

@pytest.mark.parametrize('url', ['http://wx1.sinaimg.cn/a', 'https://localhost/a', 'https://sinaimg.cn.evil.test/a', 'https://u:p@wx1.sinaimg.cn/a', 'https://wx1.sinaimg.cn:444/a', 'file:///etc/passwd', 'https://wx1.sinaimg.cn/a\nX:y'])
def test_bad_urls_rejected(url):
    assert image_url(url, 'weibo') is None

def test_extract_images_order_dedupe_and_repost():
    item = dict(id=1, text='hello', pics=[{'large': {'url': URL}}, {'url': URL}], retweeted_status={'pics': [{'url': '//wx2.sinaimg.cn/large/original.jpg'}]})
    assert extract_images('weibo', item) == [URL, 'https://wx2.sinaimg.cn/large/original.jpg']
    assert normalize_post('weibo', '1', item, 'name')['images'] == extract_images('weibo', item)
    assert is_uncommented_weibo_repost(dict(item, text='转发微博'))
    html = '<img class="emoji" src="https://xqimg.com/face/a.png"><img src="//xqimg.com/a.jpg"><img data-original="https://xqimg.com/b.png" src="https://xqimg.com/thumb.png">'
    assert extract_images('xueqiu', {'text': html}) == ['https://xqimg.com/a.jpg', 'https://xqimg.com/b.png']

def test_picture_cap_and_unknown_payload():
    assert len(extract_images('weibo', {'pics': [{'url': URL+str(i)} for i in range(40)]})) == 18
    assert extract_images('weibo', {'pics': None, 'pic_infos': []}) == []

@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'])
def test_download_denies_private_dns(ip):
    with patch('app.services.social_images.socket.getaddrinfo', return_value=[(None,None,None,None,(ip,443))]), patch('app.services.social_images.PinnedHTTPSConnection') as connection:
        with pytest.raises(ValueError): download_image(URL, 'weibo')
        connection.assert_not_called()

def response_for(status=200, mime='image/jpeg', length='3', data=b'\xff\xd8\xff'):
    result = Mock(status=status)
    result.getheader.side_effect = lambda key: {'Content-Type':mime, 'Content-Length':length}.get(key)
    result.read1.side_effect = [data, b'']
    return result

@pytest.mark.parametrize('response', [response_for(status=302), response_for(mime='text/html'), response_for(length=str(MAX_BYTES+1)), response_for(data=b'<html>'), response_for(length=None, data=b'x'*(MAX_BYTES+1))])
def test_download_bounds(response):
    with patch('app.services.social_images.socket.getaddrinfo', return_value=[(None,None,None,None,('8.8.8.8',443))]), patch('app.services.social_images.PinnedHTTPSConnection') as connection:
        connection.return_value.getresponse.return_value = response
        with pytest.raises(ValueError): download_image(URL, 'weibo')
        connection.return_value.close.assert_called_once()

def test_download_pins_public_address_and_returns_bytes():
    with patch('app.services.social_images.socket.getaddrinfo', return_value=[(None,None,None,None,('8.8.8.8',443))]), patch('app.services.social_images.PinnedHTTPSConnection') as connection:
        connection.return_value.getresponse.return_value = response_for()
        assert download_image(URL, 'weibo') == (b'\xff\xd8\xff', 'image/jpeg')
        connection.assert_called_once_with('wx1.sinaimg.cn', '8.8.8.8')
        assert 'Cookie' not in connection.return_value.request.call_args.kwargs['headers']

def test_text_first_then_images_and_no_replay(channels):
    _, service, text, feishu = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL, URL+'2'])])
    sequence = []
    text.side_effect = lambda *args: sequence.append('text')
    with patch.object(service, 'send_photo', side_effect=lambda *args, **kwargs: sequence.append('image')) as photo:
        service.deliver_notifications(config(service)); service.deliver_notifications(config(service))
    assert sequence == ['text', 'image', 'image']
    assert photo.call_count == 2 and feishu.call_count == 1
    assert deliveries(service)[0]['status'] == 'sent'

def test_failed_photo_does_not_repeat_text_or_stop_later_photos(channels):
    _, service, text, feishu = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL, URL+'2'])])
    with patch.object(service, 'send_photo', side_effect=[RuntimeError('timeout'), None]) as photo:
        service.deliver_notifications(config(service)); service.deliver_notifications(config(service))
    assert text.call_count == 1 and photo.call_count == 2 and feishu.call_count == 1
    state = deliveries(service)[0]['delivery']['telegram']
    assert state['status'] == 'sent' and state['image_warning']

def test_crash_after_attempt_checkpoint_never_replays_image(channels):
    _, service, text, _ = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL])])
    with patch.object(service, 'send_photo', side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt): service.deliver_notifications(config(service))
    assert deliveries(service)[0]['delivery']['telegram']['next_part'] == 2
    with patch.object(service, 'send_photo') as photo:
        service.deliver_notifications(config(service)); photo.assert_not_called()
    assert text.call_count == 1
    assert deliveries(service)[0]['delivery']['telegram']['status'] == 'sent'

def test_text_failure_retries_before_any_photo(channels):
    _, service, text, _ = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL])])
    text.side_effect = RuntimeError('offline')
    with patch.object(service, 'send_photo') as photo:
        with pytest.raises(RuntimeError): service.deliver_notifications(config(service))
        photo.assert_not_called()
        text.side_effect = None
        service.deliver_notifications(config(service)); photo.assert_called_once()

def test_disabled_channel_does_not_send_photos(channels):
    _, service, text, _ = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL])])
    service.update_config({'telegram_enabled': False})
    with patch.object(service, 'send_photo') as photo:
        service.deliver_notifications(config(service)); photo.assert_not_called()
    text.assert_not_called()

def test_photo_lock_or_limit_before_attempt_remains_pending(channels):
    from app.services.social_notifications import NotificationError
    _, service, text, _ = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL])])
    real = service.send_channel
    def send(channel, part, **kwargs):
        if isinstance(part, dict): raise NotificationError('busy before guard')
        return real(channel, part, **kwargs)
    with patch.object(service, 'send_channel', side_effect=send):
        with pytest.raises(RuntimeError): service.deliver_notifications(config(service))
    assert deliveries(service)[0]['delivery']['telegram']['next_part'] == 1
    with patch.object(service, 'send_photo') as photo:
        service.deliver_notifications(config(service)); photo.assert_called_once()
    assert text.call_count == 1

def test_pause_during_download_prevents_upload_and_keeps_image_pending(channels):
    _, service, text, _ = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL])])
    def download(*args):
        service.update_config({'enabled': False})
        return b'\xff\xd8\xff', 'image/jpeg'
    with patch('app.services.social_images.download_image', side_effect=download), patch('app.services.social.httpx.Client') as client:
        service.deliver_notifications(config(service))
        client.assert_not_called()
    assert deliveries(service)[0]['delivery']['telegram']['next_part'] == 1
    service.update_config({'enabled': True})
    with patch.object(service, 'send_photo') as photo:
        service.deliver_notifications(config(service)); photo.assert_called_once()
    assert text.call_count == 1

def test_photo_transport_uploads_bytes_not_cookie_or_remote_url(channels):
    _, service, _, _ = channels
    with patch('app.services.social_images.download_image', return_value=(b'\xff\xd8\xff', 'image/jpeg')), patch('app.services.social.httpx.Client') as client:
        response = client.return_value.__enter__.return_value.post.return_value
        response.is_success = True
        response.json.return_value = {'ok': True}
        guard = Mock()
        service.send_photo('fake-token', '123', {'url':URL, 'platform':'weibo', 'caption':'配图 1/1'}, before_upload=guard)
        guard.assert_called_once()
        call = client.return_value.__enter__.return_value.post.call_args
        assert call.args[0].endswith('/sendPhoto')
        assert call.kwargs['files']['photo'][1] == b'\xff\xd8\xff'
        assert call.kwargs['data'] == {'chat_id':'123','caption':'配图 1/1'}
        assert client.call_args.kwargs['trust_env'] is False

def test_destination_change_during_download_defers_old_target(channels):
    _, service, text, _ = channels
    author = enable(service)
    service.collect(author, [dict(post(), images=[URL])])
    def download(*args):
        service.update_config({'chat_id': '456'})
        return b'\xff\xd8\xff', 'image/jpeg'
    with patch('app.services.social_images.download_image', side_effect=download), patch('app.services.social.httpx.Client') as client:
        service.deliver_notifications(config(service)); client.assert_not_called()
    assert deliveries(service)[0]['delivery']['telegram']['next_part'] == 1
    with patch.object(service, 'send_photo') as photo:
        service.deliver_notifications(config(service))
        assert photo.call_args.args[1] == '456'

def test_download_body_deadline_closes_connection():
    with patch('app.services.social_images.socket.getaddrinfo', return_value=[(None,None,None,None,('8.8.8.8',443))]), patch('app.services.social_images.PinnedHTTPSConnection') as connection, patch('app.services.social_images.time.monotonic', side_effect=[0,31]):
        connection.return_value.getresponse.return_value = response_for()
        with pytest.raises(ValueError, match='timed out'): download_image(URL, 'weibo')
        connection.return_value.close.assert_called_once()
