"""One browser navigation for comparison; no profile/credential/monitor state changes."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))

import httpx

from app.core.config import Settings
from app.services.social_browser import allowed_url, browser_error
from app.services.social_sources import XUEQIU_PAGE_SIZE
from scripts.check_xueqiu_response import ENDPOINT, MAX_BYTES, classify, stored_request


def browser_cookies(header):
    cookies = []
    for part in header.split(';'):
        if not part.strip():
            continue
        name, separator, value = part.strip().partition('=')
        if not separator or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
            raise ValueError('invalid_cookie_format')
        cookies.append({'name': name, 'value': value, 'domain': '.xueqiu.com',
                        'path': '/', 'secure': True})
    if not cookies:
        raise ValueError('invalid_cookie_format')
    return cookies


def probe(context, uid, cookie):
    context.add_cookies(browser_cookies(cookie))
    responses = []
    counts = {'requests': 0, 'documents': 0}
    deadline = time.monotonic() + 45

    def route_request(route):
        counts['requests'] += 1
        if route.request.is_navigation_request():
            counts['documents'] += 1
        if (counts['requests'] > 40 or counts['documents'] > 4 or time.monotonic() > deadline
                or not allowed_url('xueqiu', route.request.url)):
            route.abort()
        else:
            route.continue_()

    context.route('**/*', route_request)
    context.route_web_socket('**/*', lambda socket: socket.close())
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(10000)
    page.on('dialog', lambda dialog: dialog.dismiss())

    def received(response):
        if response.request.is_navigation_request() and response.request.frame == page.main_frame:
            responses.append(response)

    page.on('response', received)
    page.goto(ENDPOINT + '?' + urlencode({'user_id': uid, 'page': 1, 'count': XUEQIU_PAGE_SIZE}),
              wait_until='domcontentloaded', timeout=30000, referer='https://www.xueqiu.com/')
    page.wait_for_timeout(3000)
    if not responses:
        return {'result': 'no_document_response', 'message': '浏览器未获得主页面响应'}
    response = responses[-1]
    body = response.body()
    sample = httpx.Response(response.status, content=body[:MAX_BYTES], headers={
        'Content-Type': response.headers.get('content-type', ''),
        'Location': response.headers.get('location', ''),
    })
    code, message = classify(sample)
    if code in ('unidentified_html', 'non_json'):
        # Read the rendered page as well; JavaScript may have displayed an explicit error.
        visible = page.locator('body').inner_text(timeout=10000)[:MAX_BYTES]
        try:
            json.loads(visible)
        except ValueError:
            rendered = httpx.Response(200, text=page.content()[:MAX_BYTES])
        else:
            rendered = httpx.Response(200, text=visible)
        rendered_code, rendered_message = classify(rendered)
        if rendered_code != 'unidentified_html' and rendered_code != 'non_json':
            code, message = rendered_code, rendered_message
    host = urlsplit(page.url).hostname
    return {'mode': 'chromium', 'cookie_loaded': True, 'http_status': response.status,
            'body_truncated': len(body) > MAX_BYTES, 'result': code, 'message': message,
            'final_site': 'xueqiu' if host in ('xueqiu.com', 'www.xueqiu.com') else 'other',
            'document_requests': counts['documents']}


def main():
    stage = '读取本地配置'
    try:
        settings = Settings()
        uid, cookie = stored_request(settings)
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.runtime' / 'playwright'))
        from playwright.sync_api import sync_playwright
        stage = '启动浏览器'
        with tempfile.TemporaryDirectory(prefix='xueqiu-check-', dir=settings.log_path) as profile:
            with sync_playwright() as playwright:
                options = {'headless': True, 'chromium_sandbox': os.name != 'nt',
                           'accept_downloads': False, 'service_workers': 'block'}
                if settings.social_browser_channel:
                    options['channel'] = settings.social_browser_channel
                context = playwright.chromium.launch_persistent_context(profile, **options)
                try:
                    stage = '打开帖子接口'
                    result = probe(context, uid, cookie)
                finally:
                    context.close()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['result'] == 'posts_json' else 2
    except Exception as error:
        if stage == '读取本地配置':
            code, message = 'local_state_error', '无法读取本地博主或解密已保存的 Cookie，请检查配置路径和权限'
        elif isinstance(error, ValueError) and str(error) == 'invalid_cookie_format':
            code, message = 'invalid_cookie_format', '保存的 Cookie 格式异常，应填写请求标头 Cookie 的值，不带 Cookie: 前缀'
        else:
            code, message = browser_error(error)
        print(json.dumps({'mode': 'chromium', 'stage': stage, 'result': code, 'message': message}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
