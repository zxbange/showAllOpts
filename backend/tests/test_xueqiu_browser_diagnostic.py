import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

spec = importlib.util.spec_from_file_location('xueqiu_browser_diagnostic', Path(__file__).resolve().parents[2] / 'scripts/check_xueqiu_browser.py')
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def test_cookies_keep_equals_and_are_scoped_to_platform():
    cookies = diagnostic.browser_cookies('xq_a_token=a=b; u=123; ')
    assert cookies[0]['value'] == 'a=b'
    assert all(item['domain'] == '.xueqiu.com' and item['secure'] for item in cookies)
    with pytest.raises(ValueError, match='invalid_cookie_format'):
        diagnostic.browser_cookies('Cookie: xq_a_token=secret')


@pytest.mark.parametrize('body,rendered,expected', [
    (b'{"statuses":[]}', '', 'posts_json'),
    (b'<html><body></body></html>', '<html>安全策略拦截 secret-cookie</html>', 'security_block_page'),
    (b'<html><body></body></html>', '<html><title>登录</title></html>', 'login_page'),
    (b'<html><body></body></html>', '<html>普通页面</html>', 'unidentified_html'),
])
def test_browser_probe_classifies_without_exposing_cookie_or_body(body, rendered, expected):
    context, page, response = MagicMock(), MagicMock(), MagicMock()
    context.pages = [page]
    response.request.is_navigation_request.return_value = True
    response.request.frame = page.main_frame
    response.status = 200
    response.headers = {'content-type': 'text/html'}
    response.body.return_value = body
    page.url = 'https://xueqiu.com/v4/statuses/user_timeline.json?user_id=private'
    page.on.side_effect = lambda event, callback: callback(response) if event == 'response' else None
    page.locator.return_value.inner_text.return_value = '普通页面'
    page.content.return_value = rendered
    result = diagnostic.probe(context, '123', 'u=secret-cookie')
    assert result['result'] == expected
    assert 'secret-cookie' not in str(result) and 'private' not in str(result)
    page.goto.assert_called_once()
    context.add_cookies.assert_called_once()
