import importlib.util
import gzip
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

spec = importlib.util.spec_from_file_location('xueqiu_diagnostic', Path(__file__).resolve().parents[2] / 'scripts/check_xueqiu_response.py')
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


@pytest.mark.parametrize('status,body,expected', [
    (200, '<html>请求异常已被安全策略拦截，如未登录请先登录</html>', 'security_block_page'),
    (200, '<html>请完成人机验证</html>', 'verification_page'),
    (200, '<html><title>登录雪球</title></html>', 'login_page'),
    (200, '<html>欢迎访问雪球<button>登录</button></html>', 'unidentified_html'),
    (200, '<html><script>const msg="access denied";</script>普通页面</html>', 'unidentified_html'),
    (200, '{"statuses":[]}', 'posts_json'),
    (200, '{"statuses":[{"text":"访问受限，请先登录"}]}', 'posts_json'),
    (200, '{"error_code":"400016"}', 'login_expired'),
    (403, 'private-body', 'access_denied'),
    (401, 'private-body', 'login_required'),
    (429, 'private-body', 'rate_limited'),
])
def test_classification_never_exposes_body(status, body, expected):
    response = httpx.Response(status, text=body + ('<!-- secret-cookie -->' if body.startswith('<') else ''))
    code, message = diagnostic.classify(response)
    assert code == expected
    assert 'secret-cookie' not in message and 'private-body' not in message


def test_redirect_is_not_followed_or_printed():
    response = httpx.Response(302, headers={'location': 'https://www.xueqiu.com/v4/statuses/user_timeline.json?token=private'})
    assert diagnostic.classify(response)[0] == 'api_host_redirect'
    assert 'private' not in str(diagnostic.classify(response))


def test_one_request_no_retry_no_state_change(capsys):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text='<html>安全策略拦截</html>secret-cookie')
    transport = httpx.MockTransport(respond)
    real_client = httpx.Client
    with patch.object(diagnostic, 'stored_request', return_value=('123', 'SUB=secret-cookie')), patch.object(
            diagnostic.httpx, 'Client', side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)) as client:
        assert diagnostic.main() == 2
    client.assert_called_once()
    assert len(requests) == 1
    assert requests[0].headers['Cookie'] == 'SUB=secret-cookie'
    output = capsys.readouterr().out
    assert 'security_block_page' in output
    assert 'secret-cookie' not in output


def test_compressed_json_is_decoded_once(capsys):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'Content-Encoding': 'gzip', 'Content-Type': 'application/json'},
        content=gzip.compress(b'{"statuses":[]}')))
    real_client = httpx.Client
    with patch.object(diagnostic, 'stored_request', return_value=('123', 'test-cookie')), patch.object(
            diagnostic.httpx, 'Client', side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
        assert diagnostic.main() == 0
    assert 'posts_json' in capsys.readouterr().out
