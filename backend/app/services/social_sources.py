import html
import re
import random
import math
from html.parser import HTMLParser
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from contextlib import closing
from email.utils import parsedate_to_datetime
import httpx


class PlatformIntervention(RuntimeError):
    """A platform requires the operator to log in/verify manually."""

    def __init__(self, message, reason_code='platform_restricted', http_status=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status


class RateLimited(RuntimeError):
    def __init__(self, retry_after=1800):
        super().__init__('平台请求限流，已暂缓抓取')
        seconds = float(retry_after)
        self.retry_after = max(1, seconds) if math.isfinite(seconds) else 1800


class MonitoringDeferred(RuntimeError):
    pass


class PostBatch(list):
    def __init__(self, posts, complete=True):
        super().__init__(posts)
        self.complete = complete


def check_response(response):
    if response.status_code == 429:
        retry = response.headers.get('Retry-After', '')
        try:
            seconds = float(retry)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 1800
        raise RateLimited(seconds)
    if response.status_code == 401:
        raise PlatformIntervention('登录凭据失效或未登录（HTTP 401），请重新登录平台并更新登录凭据', 'login_expired', 401)
    if response.status_code in (403, 418):
        raise PlatformIntervention(f'平台拒绝访问（HTTP {response.status_code}），请在浏览器检查访问权限或完成安全验证', 'access_denied', response.status_code)
    if response.is_redirect:
        raise PlatformIntervention(f'平台跳转至登录或验证页面（HTTP {response.status_code}），请在浏览器检查登录状态', 'login_redirect', response.status_code)
    if response.status_code != 200:
        try:
            payload = response.json()
        except ValueError:
            check_payload({'message': plain_text(response.text)[:4000]})
        else:
            if isinstance(payload, dict):
                check_payload(payload)
        raise RuntimeError(f'平台 HTTP {response.status_code}，稍后退避重试')


def check_payload(payload):
    # Inspect error fields only: a post mentioning login/captcha is normal content.
    fields = ('msg', 'message', 'error', 'error_description', 'errmsg', 'description')
    message = ' '.join(str(payload.get(key, '')) for key in fields).lower()
    if any(word in message for word in ('验证码', '安全验证', '人机验证', 'captcha')):
        raise PlatformIntervention('平台要求验证码或安全验证，请在浏览器完成人工验证后恢复监控', 'captcha_required')
    if any(word in message for word in ('请先登录', '请登录', '重新登录', '登录失效', '登录过期', '登录超时',
                                        '登陆失效', '登陆过期', '未登录', 'login required', 'login expired',
                                        'not logged', 'unauthorized', 'invalid cookie', 'session expired', 'token expired')):
        raise PlatformIntervention('平台登录已失效或未登录，请重新登录并更新登录凭据后恢复监控', 'login_expired')
    if any(word in message for word in ('访问拒绝', '访问受限', '禁止访问', 'access denied', 'forbidden')):
        raise PlatformIntervention('平台拒绝访问或限制当前账号，请在浏览器检查账号权限及安全提示', 'access_denied')
    if str(payload.get('error_code', '')) in ('21301', '21314', '21315', '21327', '400016'):
        raise PlatformIntervention(f"平台登录凭据失效（错误码 {payload['error_code']}），请重新登录并更新登录凭据", 'login_expired')
    if any(word in message for word in ('频繁', '请求过多', 'too many requests', 'rate limit')):
        raise RateLimited()


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("br", "p", "div"):
            self.parts.append("\n")
        if tag == "img" and attrs.get("alt"):
            self.parts.append(attrs["alt"])

    def handle_endtag(self, tag):
        if tag in ("p", "div"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    parser = TextParser()
    parser.feed(str(value or ""))
    return re.sub(r"\n{3,}", "\n\n", html.unescape("".join(parser.parts))).strip()


def author_uid(author):
    value = str(author.get("uid") or author.get("url") or "").strip()
    if value.isdigit():
        return value
    parsed = urlparse(value)
    hosts = {"weibo": {"weibo.com", "www.weibo.com", "m.weibo.cn"},
             "xueqiu": {"xueqiu.com", "www.xueqiu.com"}}
    if parsed.hostname not in hosts[author["platform"]]:
        raise ValueError("博主链接域名与 platform 不匹配")
    match = re.fullmatch(r"/(?:u/)?(\d+)/?", parsed.path)
    if not match:
        raise ValueError("请配置数字 uid，或包含数字 UID 的博主主页链接")
    return match.group(1)


def fetch_json(session, url, **kwargs):
    with closing(session.get(url, timeout=30, **kwargs)) as response:
        check_response(response)
        try:
            payload = response.json()
        except ValueError:
            check_payload({'message': plain_text(response.text)[:4000]})
            if '<html' in response.text[:1000].lower() or '<!doctype' in response.text[:1000].lower():
                raise PlatformIntervention('接口返回网页而非帖子数据，可能被引导至登录或验证页面，请在浏览器人工检查', 'unexpected_html') from None
            raise RuntimeError("平台返回非 JSON 内容，可能需要登录或人工验证") from None
    if not isinstance(payload, dict):
        raise RuntimeError("平台响应结构异常")
    check_payload(payload)
    return payload


def walk_cards(cards):
    for card in cards:
        if "mblog" in card:
            yield card["mblog"]
        yield from walk_cards(card.get("card_group", []))


def normalize_post(platform, uid, item, fallback_name):
    post_id = str(item["id"])
    user = item.get("user") or {}
    body = plain_text(item.get("text") or item.get("description"))
    title = plain_text(item.get("title"))
    if title:
        body = title + "\n" + body
    original = item.get("retweeted_status")
    if original:
        origin_name = (original.get("user") or {}).get("screen_name", "原作者")
        body += f"\n\n转发 @{origin_name}：\n" + plain_text(original.get("text") or original.get("description"))
    raw_time = item.get("created_at", "")
    published = str(raw_time)
    try:
        if platform == "xueqiu":
            moment = datetime.fromtimestamp(float(raw_time) / 1000, timezone.utc)
        else:
            moment = datetime.strptime(str(raw_time), "%a %b %d %H:%M:%S %z %Y")
        published = moment.astimezone(timezone(timedelta(hours=8))).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return {
        "id": post_id,
        "name": user.get("screen_name") or fallback_name or uid,
        "time": published,
        "text": body or "[图片或视频帖子，请查看原文]",
        "url": (f"https://m.weibo.cn/detail/{post_id}" if platform == "weibo"
                else f"https://xueqiu.com/{uid}/{post_id}"),
    }


def fetch_posts(author, pages):
    platform, uid = author["platform"], author["uid"]
    known = set(author.get('_known_ids', ()))
    since = author.get('_scan_since')
    since = datetime.fromisoformat(since) if since else None
    cutoff = datetime.fromisoformat(author['_retention_cutoff']) if author.get('_retention_cutoff') else None
    stop_before = datetime.fromisoformat(author['_stop_before']) if author.get('_stop_before') else None
    resume = max(1, author.get('_scan_page', 1))
    initial = author.get('_initial', True)
    callback = author.get('_on_page')
    request_count = 0
    author_name_reported = False

    def guard(_request):
        nonlocal request_count
        if request_count and author.get('_wait', lambda _seconds: False)(random.uniform(1, 3)):
            raise MonitoringDeferred('监控进程正在停止')
        if author.get('_should_stop', lambda: False)():
            raise MonitoringDeferred('监控已暂停或进入夜间休眠')
        request_count += 1

    with httpx.Client(follow_redirects=False, event_hooks={'request': [guard]}) as session:
        session.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
                                "Referer": "https://m.weibo.cn/" if platform == "weibo" else "https://xueqiu.com/"})
        cookie = author.get("cookie", "")
        if cookie:
            session.headers["Cookie"] = cookie
        elif platform == "xueqiu":
            # Obtain ordinary visitor cookies; no verification bypass is attempted.
            with closing(session.get("https://xueqiu.com/", timeout=30)) as response:
                check_response(response)
        found = {}
        page_numbers = ([1] if resume > 1 else []) + list(range(resume, resume + pages - int(resume > 1)))
        complete = False
        for page in page_numbers:
            if author.get('_should_stop', lambda: False)():
                raise MonitoringDeferred('监控已暂停或进入夜间休眠')
            if platform == "weibo":
                data = fetch_json(session, "https://m.weibo.cn/api/container/getIndex", params={
                    "type": "uid", "value": uid, "containerid": "107603" + uid, "page": page})
                if data.get("ok") != 1 or not isinstance(data.get("data"), dict) or "cards" not in data["data"]:
                    raise RuntimeError("微博未返回有效帖子列表，请检查 UID、Cookie 或访问限制")
                items = list(walk_cards(data["data"]["cards"]))
            else:
                data = fetch_json(session, "https://xueqiu.com/v4/statuses/user_timeline.json", params={
                    "user_id": uid, "page": page, "count": 20})
                if not isinstance(data.get("statuses"), list) or data.get("error_code"):
                    raise RuntimeError("雪球未返回有效帖子列表，请检查 UID、Cookie 或访问限制")
                items = data["statuses"]
            page_posts, boundary = [], False
            for item in items:
                # Exclude recommendations belonging to a different author.
                owner = (item.get("user") or {}).get("id") or item.get("user_id")
                if owner is not None and str(owner) != uid:
                    continue
                identifier = str(item['id'])
                screen_name = (item.get('user') or {}).get('screen_name')
                if screen_name and not author_name_reported and author.get('_on_author_name'):
                    author['_on_author_name'](screen_name)
                    author_name_reported = True
                preview = normalize_post(platform, uid, item, author.get('name'))
                try:
                    timestamp = datetime.fromisoformat(preview['time'])
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone(timedelta(hours=8)))
                except ValueError:
                    timestamp = None
                pinned = any(item.get(key) for key in ('isTop', 'is_top', 'is_topped', 'top')) or '置顶' in str((item.get('title') or {}))
                if cutoff and timestamp and timestamp < cutoff:
                    if not pinned:
                        boundary = True
                    continue
                if not pinned and identifier in known and (since is None or (timestamp and timestamp <= since)):
                    boundary = True
                if not pinned and stop_before and timestamp and timestamp < stop_before:
                    boundary = True
                if identifier in known:
                    continue
                if platform == 'weibo' and item.get('isLongText'):
                    detail = fetch_json(session, 'https://m.weibo.cn/statuses/extend', params={'id': item['id']})
                    content = detail.get('data', {}).get('longTextContent')
                    if detail.get('ok') != 1 or not content:
                        raise RuntimeError('微博长文读取失败，保留到下轮重试')
                    item['text'] = content
                post = normalize_post(platform, uid, item, author.get("name"))
                found[post["id"]] = post
                page_posts.append(post)
            # The newest page is rechecked during a resumed scan, without losing
            # the deeper cursor or mistaking the new checkpoint rows for its end.
            top_recheck = resume > 1 and page == 1
            complete = not top_recheck and (not items or boundary or (initial and page == page_numbers[-1]))
            next_page = 1 if complete else resume if top_recheck else page + 1
            if callback:
                callback(page_posts, next_page, complete)
            if complete:
                break
    return PostBatch(sorted(found.values(), key=lambda post: int(post["id"])), complete)


def message_parts(platform, post):
    label = "微博" if platform == "weibo" else "雪球"
    text = f"【{label}】{post['name']}\n时间：{post['time']}\n\n{post['text']}\n\n原文：{post['url']}"
    # Telegram limits UTF-16 code units; leave room for astral characters.
    parts, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 3500:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        parts.append("".join(current))
    return parts
