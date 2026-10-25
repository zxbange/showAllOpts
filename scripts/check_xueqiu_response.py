"""One read-only request with stored credentials; never print response bodies or secrets."""
import json
import logging
from pathlib import Path
import sqlite3
import sys
from html.parser import HTMLParser
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

import httpx
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.services.social_sources import check_payload, PlatformIntervention, RateLimited

ENDPOINT = 'https://xueqiu.com/v4/statuses/user_timeline.json'
MAX_BYTES = 512 * 1024


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.in_title = False
        self.title, self.parts = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title.append(data)


def classify(response):
    status = response.status_code
    if status == 401:
        return 'login_required', '服务器收到 HTTP 401，登录态未被接受'
    if status in (403, 418):
        return 'access_denied', '服务器收到访问拒绝响应'
    if status == 429:
        return 'rate_limited', '平台返回请求限流'
    if response.is_redirect:
        target = urlsplit(response.headers.get('location', ''))
        if any(word in target.path.lower() for word in ('login', 'signin', 'passport')):
            return 'login_redirect', '接口跳转至登录路径（未跟随跳转）'
        if target.hostname in ('xueqiu.com', 'www.xueqiu.com') and target.path == urlsplit(ENDPOINT).path:
            return 'api_host_redirect', '接口跳转到雪球同路径地址（未跟随跳转），需检查域名适配'
        return 'other_redirect', '接口发生跳转（未跟随跳转），不能据此判定 Cookie 失效'
    try:
        payload = response.json()
    except ValueError:
        parser = VisibleText()
        parser.feed(response.text)
        text, title = ' '.join(parser.parts).lower(), ' '.join(parser.title).lower()
        if any(word in text for word in ('安全策略拦截', '访问被拒绝', '访问受限', '禁止访问', 'access denied', 'request blocked')):
            return 'security_block_page', '返回网页包含明确的访问限制提示，不能仅靠更新 Cookie 判断能否解除'
        if any(word in text for word in ('请输入验证码', '请完成安全验证', '人机验证', 'captcha challenge')):
            return 'verification_page', '返回网页包含人工验证提示'
        if any(word in title for word in ('登录', 'login', 'sign in')) or any(
                word in text for word in ('请先登录', '登录已过期', '登录失效', 'login required')):
            return 'login_page', '返回网页包含登录要求，保存的登录态可能未被服务器接受'
        if '<html' in response.text[:1000].lower() or '<!doctype' in response.text[:1000].lower():
            return 'unidentified_html', '收到 HTML，但没有明确的登录或拦截标志；也可能是脚本挑战、普通页面或接口变化，暂不能确定'
        return 'non_json', '返回内容不是 JSON，无法识别具体原因'
    if not isinstance(payload, dict):
        return 'unexpected_json', 'JSON 结构与帖子接口不符'
    try:
        check_payload(payload)
    except PlatformIntervention as error:
        return error.reason_code, '接口 JSON 明确报告登录、验证或访问问题'
    except RateLimited:
        return 'rate_limited', '接口 JSON 报告请求限流'
    if status == 200 and isinstance(payload.get('statuses'), list) and not payload.get('error_code'):
        return 'posts_json', '本次请求返回正常帖子列表；未输出帖子内容'
    return 'unexpected_json', '收到 JSON，但不是正常帖子列表；不输出原文或错误字段'


def stored_request(settings):
    root = settings.log_path / 'social'
    with sqlite3.connect((root / 'monitor.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        config = json.loads(db.execute('SELECT payload FROM config WHERE id=1').fetchone()[0])
        author = db.execute("SELECT uid FROM authors WHERE platform='xueqiu' ORDER BY enabled DESC,id LIMIT 1").fetchone()
    if not author or not str(author[0]).isdigit():
        raise ValueError('missing_author')
    if not config.get('xueqiu_cookie'):
        raise ValueError('missing_cookie')
    cipher = Fernet((root / 'secrets.key').read_bytes())
    return str(author[0]), cipher.decrypt(config['xueqiu_cookie'].encode()).decode()


def main():
    # Avoid HTTPX INFO logging of URLs, and never print exception messages/tracebacks.
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    logging.getLogger('httpcore').setLevel(logging.CRITICAL)
    try:
        uid, cookie = stored_request(Settings())
    except ValueError as error:
        code = str(error) if str(error) in ('missing_author', 'missing_cookie') else 'local_state_error'
        print(json.dumps({'result': code, 'message': '请检查已保存的雪球博主、Cookie 及配置路径'}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({'result': 'local_state_error', 'message': '无法读取已保存的监控数据或解密 Cookie，请核对配置路径和服务账号权限'}, ensure_ascii=False))
        return 1
    try:
        with httpx.Client(follow_redirects=False, timeout=30) as client:
            # Keep request headers/parameters aligned with the production fetcher.
            with client.stream('GET', ENDPOINT, params={'user_id': uid, 'page': 1, 'count': 20}, headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148',
                'Referer': 'https://xueqiu.com/', 'Cookie': cookie,
            }) as response:
                body = bytearray()
                truncated = False
                for chunk in response.iter_bytes():
                    room = MAX_BYTES - len(body)
                    body.extend(chunk[:room])
                    if len(chunk) > room:
                        truncated = True
                        break
                # iter_bytes already decoded compression; do not decode the sampled body twice.
                media_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                sample = httpx.Response(response.status_code, headers={
                    'Content-Type': response.headers.get('content-type', ''),
                    'Location': response.headers.get('location', ''),
                },
                                        content=bytes(body), request=response.request)
                code, message = classify(sample)
                print(json.dumps({'cookie_loaded': True, 'http_status': response.status_code,
                                  'content_type': media_type if media_type in ('application/json', 'text/html', 'text/plain') else 'other',
                                  'body_truncated': truncated, 'result': code, 'message': message}, ensure_ascii=False))
        return 0 if code == 'posts_json' else 2
    except httpx.TimeoutException:
        code = 'network_timeout'
    except httpx.HTTPError:
        code = 'network_error'
    except Exception:
        code = 'diagnostic_error'
    print(json.dumps({'result': code, 'message': '诊断未完成；未重试，也未改变监控状态'}, ensure_ascii=False))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
