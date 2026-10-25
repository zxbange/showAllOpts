"""Read-only Xueqiu portfolio events and plain-text Telegram messages."""
from datetime import datetime, timedelta, timezone
import math
import random
import re
from urllib.parse import urlsplit

import httpx

from app.services.social_sources import (
    MonitoringDeferred, PostBatch, fetch_json, plain_text,
)


CHINA = timezone(timedelta(hours=8))
HISTORY_URL = 'https://www.xueqiu.com/cubes/rebalancing/history.json'
PAGE_SIZE = 20
MAX_PAGES = 20


def cube_symbol(value):
    """Extract an identifier, never use a supplied URL as a request target."""
    if not isinstance(value, str):
        raise ValueError('请填写雪球组合代码或组合主页链接')
    value = value.strip()
    if re.fullmatch(r'ZH[0-9]{1,20}', value, re.IGNORECASE):
        return value.upper()
    try:
        parsed = urlsplit(value)
        allowed = (parsed.scheme in ('http', 'https')
                   and parsed.hostname in ('xueqiu.com', 'www.xueqiu.com')
                   and parsed.port is None and parsed.username is None and parsed.password is None)
    except ValueError:
        allowed = False
    if allowed:
        match = re.fullmatch(r'/P/(ZH[0-9]{1,20})/?', parsed.path, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    raise ValueError('请填写 ZH 开头的组合代码，或雪球 /P/ZH… 组合主页链接')


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _first_number(*values):
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _time(value):
    if value is None or isinstance(value, bool):
        return ''
    try:
        number = _number(value)
        if number is not None:
            moment = datetime.fromtimestamp(number / 1000 if abs(number) >= 100_000_000_000 else number, CHINA)
        else:
            moment = datetime.fromisoformat(str(value))
            moment = moment.replace(tzinfo=CHINA) if moment.tzinfo is None else moment.astimezone(CHINA)
        return moment.isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError, OSError):
        return ''


def normalize_rebalance(symbol, record):
    identifier = str(record.get('id', ''))
    if not identifier.isascii() or not identifier.isdigit():
        raise RuntimeError('雪球组合调仓记录缺少有效编号，已保留原检查进度')
    histories = record.get('rebalancing_histories')
    if histories is None:
        histories = []
    if not isinstance(histories, list) or any(not isinstance(row, dict) for row in histories):
        raise RuntimeError('雪球组合调仓明细结构异常，已保留原检查进度')
    changes = []
    for row in histories:
        before = _first_number(row.get('prev_weight_adjusted'), row.get('prev_weight'))
        after = _first_number(row.get('target_weight'), row.get('weight'))
        if row.get('proactive') is False or before == after:
            continue
        if before is None or after is None:
            action = '调整'
        elif before == 0 and after > 0:
            action = '买入'
        elif before > 0 and after == 0:
            action = '卖出'
        else:
            action = '加仓' if after > before else '减仓'
        changes.append({
            'symbol': str(row.get('stock_symbol') or ''),
            'name': plain_text(row.get('stock_name') or row.get('stock_symbol') or '未知股票'),
            'price': _number(row.get('price')), 'before': before, 'after': after, 'action': action,
        })
    status = record.get('status')
    return {
        'id': identifier, 'status': status.strip().lower() if isinstance(status, str) else 'unknown',
        'category': str(record.get('category') or ''),
        'time': _time(record.get('created_at')), 'updated_at': _time(record.get('updated_at')),
        'comment': plain_text(record.get('comment')), 'changes': changes,
        'url': f'https://www.xueqiu.com/P/{symbol}',
    }


def fetch_cube_rebalances(cube, pages=MAX_PAGES):
    symbol = cube_symbol(cube['symbol'])
    known = {str(value) for value in cube.get('_known_ids', ())}
    pending = {str(value) for value in cube.get('_pending_ids', ())}
    initial = cube.get('_initial', True)
    since = _time(cube.get('_notify_since')) if cube.get('_notify_since') else ''
    if cube.get('_notify_since') and not since:
        raise RuntimeError('组合启用时间无效，已保留原检查进度')
    since = datetime.fromisoformat(since) if since else None
    baseline_only = initial and since is None
    limit = max(1, min(MAX_PAGES, int(pages)))
    resume = max(1, int(cube.get('_scan_page', 1)))
    resume_due = not baseline_only and resume > 1
    callback = cube.get('_on_page')
    requests = 0

    def guard(_request):
        nonlocal requests
        if requests and cube.get('_wait', lambda _seconds: False)(random.uniform(1, 3)):
            raise MonitoringDeferred('组合监控进程正在停止')
        if cube.get('_should_stop', lambda: False)():
            raise MonitoringDeferred('组合监控已暂停或不在监控时段')
        requests += 1

    found = {}
    complete = False
    page = 1
    with httpx.Client(follow_redirects=False, event_hooks={'request': [guard]}) as session:
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148',
            'Referer': f'https://www.xueqiu.com/P/{symbol}', 'Accept': 'application/json',
        })
        if cube.get('cookie'):
            session.headers['Cookie'] = cube['cookie']
        for _ in range(1 if baseline_only else limit):
            payload = fetch_json(session, HISTORY_URL, params={
                'cube_symbol': symbol, 'count': PAGE_SIZE, 'page': page,
            })
            records = payload.get('list')
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                raise RuntimeError('雪球未返回有效组合调仓列表，已保留原检查进度')
            events = [normalize_rebalance(symbol, record) for record in records]
            ids = {event['id'] for event in events}
            boundary = bool(ids & (known - pending))
            if initial and since is not None:
                # The first allowed window can follow days of unpolled changes.
                # An already saved page is only a resume marker, not the baseline.
                old_boundary = any(
                    datetime.fromisoformat(stamp) < since
                    for event in events if (stamp := event['updated_at'] or event['time'])
                )
            else:
                old_boundary = boundary
            pending.difference_update(ids)
            if page >= resume:
                resume_due = False
            complete = baseline_only or not records or (old_boundary and not pending and not resume_due)
            if complete:
                next_page = 1
            elif resume_due and boundary:
                # Check every unseen page at the front before resuming a deep scan.
                next_page = resume
                resume_due = False
            else:
                next_page = page + 1
            for event in events:
                found[event['id']] = event
            if callback:
                callback(events, next_page, complete)
            if complete:
                break
            page = next_page
    return PostBatch(sorted(found.values(), key=lambda event: int(event['id'])), complete)


def cube_message_parts(cube, event):
    symbol = cube_symbol(cube['symbol'])
    name = plain_text(cube.get('name') or symbol)
    labels = {'success': '调仓成功', 'pending': '等待完成', 'failed': '调仓失败',
              'canceled': '已撤销', 'cancelled': '已撤销'}
    lines = [f'【雪球组合调仓】{name} {symbol}',
             f"调仓时间：{event.get('time') or '未知'}（北京时间）",
             f"状态：{labels.get(event.get('status'), '待确认')}", '']
    for change in event.get('changes', ()):
        before = '未知' if change.get('before') is None else f"{change['before']:g}%"
        after = '未知' if change.get('after') is None else f"{change['after']:g}%"
        lines.append(f"{change.get('action', '调整')} {change['name']} {change['symbol']}：{before} → {after}")
        if change.get('price') is not None:
            lines.append(f"组合记录价：{change['price']:g}")
    if event.get('comment'):
        lines.extend(('', f"调仓说明：{plain_text(event['comment'])}"))
    lines.extend(('', f'原文：https://www.xueqiu.com/P/{symbol}'))
    text = '\n'.join(lines)
    parts, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 3500:
            parts.append(''.join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        parts.append(''.join(current))
    return parts
