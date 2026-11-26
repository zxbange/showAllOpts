"""Public error descriptions from fixed vocabularies; never retain exception text."""
import sqlite3
from datetime import date
import re

MESSAGES = {
    'timeout': '操作超时，请稍后重试。',
    'database_error': '数据库操作失败，请检查数据库连接与服务状态。',
    'invalid_data': '数据校验未通过，未将不完整数据作为成功结果。',
    'network_error': '数据源连接失败，请稍后重试。',
    'etf_commit_unconfirmed': 'ETF 入库提交结果未确认，请先核对数据库结果再重试。',
    'etf_missing_shares': '目标交易日的 ETF 份额未全部返回。',
    'etf_missing_prices': '目标交易日的 ETF 收盘价未全部返回。',
    'etf_missing_shares_and_prices': '目标交易日的 ETF 份额和收盘价均未全部返回。',
    'etf_szse_price_archived': '深交所已归档该交易日的基金行情，公开历史报表无法核验当日收盘价。',
    'etf_manual_schedule_invalid': '未能读取有效的 ETF 自动采集时间，手动采集未开始。',
    'etf_manual_slot_unverified': '交易日历未能核验最近已到的 ETF 自动采集时点，手动采集未开始。',
    'cancelled': '采集已取消。',
    'interrupted': '采集被中断，尚未确认完整完成。',
    'market_history_unverified': '未能核验该交易日的历史行情快照。',
    'market_heat_unverified': '尚未核验市场热度所需的复权及 MA20 口径。',
    'cci_target_not_ready': '数据源尚未返回目标日动力煤报价，请等待后续更新。',
    'margin_target_not_ready': '东方财富尚未返回该统计日有效担保比例，请等待披露后继续补采。',
    'market_source_not_ready': '数据源尚未提供该交易日可核验的行情。',
    'market_gap_pending': '市场指标仍有交易日未取得或未入库，不计为采集成功。',
    'publication_pending': '数据尚未完整发布到页面缓存。',
    'unknown': '采集未完成，未保存可公开的详细原因。',
}
STAGES = frozenset(('fetch', 'validate', 'database_write', 'cache_publish', 'collection', 'database_sync'))
OPERATIONS = frozenset(('schema', 'lock', 'read', 'write', 'commit',
                        'cache_refresh', 'cache_verify', 'local_publish'))


def sanitize_error(value):
    if not isinstance(value, dict) or value.get('code') not in MESSAGES:
        return None
    result = dict(code=value['code'], message=MESSAGES[value['code']])
    if value.get('stage') in STAGES:
        result['stage'] = value['stage']
    if isinstance(value.get('operation'), str) and value['operation'] in OPERATIONS:
        result['operation'] = value['operation']
    for key in ('mysql_errno', 'sqlite_errno'):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 65535:
            result[key] = number
    status = value.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    for key in ('trade_date', 'start_date', 'end_date'):
        value_date = value.get(key)
        if type(value_date) is date:
            result[key] = value_date.isoformat()
        elif isinstance(value_date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value_date):
            try:
                result[key] = date.fromisoformat(value_date).isoformat()
            except ValueError:
                pass
    return result


def safe_error(exc=None, *, stage=None, code=None, trade_date=None, start_date=None, end_date=None,
               operation=None):
    """Classify exception chains using types/numeric driver codes, never str(exc)."""
    import httpx
    from pymysql.err import MySQLError
    from sqlalchemy.exc import SQLAlchemyError
    result = {'code': code if code in MESSAGES else 'unknown', 'stage': stage, 'operation': operation,
              'trade_date': trade_date, 'start_date': start_date, 'end_date': end_date}
    seen = set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            result['code'] = 'timeout'
        elif result['code'] != 'timeout':
            if isinstance(exc, (SQLAlchemyError, sqlite3.Error, MySQLError)):
                result['code'] = 'database_error'
            elif isinstance(exc, httpx.HTTPError):
                result['code'] = 'network_error'
            elif isinstance(exc, ValueError) and result['code'] == 'unknown':
                result['code'] = 'invalid_data'
        if isinstance(exc, httpx.HTTPStatusError):
            result['http_status'] = exc.response.status_code
        if isinstance(exc, MySQLError) and exc.args and type(exc.args[0]) is int:
            result['mysql_errno'] = exc.args[0]
        if isinstance(exc, sqlite3.Error):
            result['sqlite_errno'] = getattr(exc, 'sqlite_errorcode', None)
        exc = getattr(exc, 'orig', None) or exc.__cause__ or exc.__context__
    return sanitize_error(result)
