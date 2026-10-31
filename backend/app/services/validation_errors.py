"""Allowlisted diagnostics: never format exception messages, SQL, or locals."""
import sqlite3
from pymysql.err import MySQLError

PHASES = {
    'worker': '任务调度', 'source_config': '读取连接配置',
    'source_schema': '检查源库表结构', 'source_plan': '读取源库日期计划',
    'source_universe': '读取源库股票清单', 'source_read': '读取源库行情',
    'connect': '连接源数据库', 'ultra_research_bar': '读取源库行情',
    'ultra_research_universe': '读取源库股票快照', 'compute': '计算信号',
    'commit': '保存计算断点', 'local_store': '读写本地任务数据库',
    'summarize': '汇总已提交结果',
}
SAFE_VALIDATIONS = {
    '任务算法版本已变化，请创建新任务',
    '任务输入模式不受当前版本支持，请创建新任务',
    '旧任务未绑定不可变数据源，请新建任务；已完成输入冻结的旧任务仍可继续',
    '股票数量超过任务上限20000，请检查源数据',
    '区间内没有合格股票快照，请检查已有采集数据',
    '单股行情超过4000行，请缩短日期区间或检查重复数据',
    '没有可用基础样本；请检查预热、行情与历史快照，不能当作零胜率',
    '任务断点冲突，请重试',
    '股票行情表为空，请先完成采集',
    '要求：开始日期≤截止日期，且区间内存在已采集的收盘交易日',
    '交易日历超过20000行，请检查重复或异常历史数据',
    '交易日历重复或乱序',
    '行情最大日期不在已采集的交易日历中',
    '开始日期之前不足160个预热交易日',
    '任务绑定的数据库配置不存在，请新建任务',
    '数据库配置无法解密或完整性校验失败，请检查原加密密钥；不会回退明文',
    '连接失败：请检查允许目标、网络、账户、库名和TLS证书；详细凭证不会返回',
}
MYSQL_CODES = {
    1040: '源数据库连接数已满，请稍后重试或检查连接容量',
    1044: '源数据库访问被拒绝，请检查账户的库访问权限',
    1045: '源数据库认证失败，请检查已绑定连接的账户和密码',
    1049: '源数据库不存在，请检查绑定的库名',
    1054: '源数据库字段不匹配，请检查研究表结构',
    1142: '源数据库读取权限不足，请检查只读账户权限',
    1146: '源数据库缺少所需表，请检查研究表结构',
    1205: '源数据库锁等待超时，请稍后重试并检查数据库负载',
    1213: '源数据库事务发生死锁，请稍后重试',
    2002: '无法建立源数据库连接，请检查地址、端口和服务状态',
    2003: '无法建立源数据库连接，请检查地址、端口和服务状态',
    2006: '源数据库连接已断开，请检查服务状态或连接超时设置',
    2013: '源数据库连接在查询期间中断，可能涉及超时、网络或服务状态，请检查后重试',
    2026: '源数据库TLS连接失败，请检查TLS设置和证书',
}
SQLITE_CODES = {
    5: '本地任务数据库繁忙或被锁定，请稍后重试',
    6: '本地任务数据库被锁定，请稍后重试',
    8: '本地任务数据库只读，请检查任务目录写权限',
    10: '本地任务数据库I/O失败，请检查服务器磁盘与文件系统',
    11: '本地任务数据库可能损坏，请先备份并由管理员检查',
    13: '本地任务数据库磁盘空间不足，请检查服务器剩余空间',
    14: '无法打开本地任务数据库，请检查目录、权限和磁盘',
}


def mark_phase(exc, phase):
    # An inner local-storage failure must not be relabelled as a source failure.
    if not hasattr(exc, '_validation_phase'):
        exc._validation_phase = phase


def chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < 12:
        seen.add(id(exc))
        yield exc
        exc = getattr(exc, 'orig', None) or exc.__cause__ or exc.__context__


def diagnostic(exc, phase):
    phase = getattr(exc, '_validation_phase', phase)
    phase = phase if phase in PHASES else 'worker'
    code = 'unknown'
    reason = ('本地任务存储操作失败，请检查服务器磁盘、权限及数据库锁' if phase == 'local_store'
              else '操作失败，原始异常内容已隐藏；请按任务标识查看服务器 error.log')
    kind = type(exc).__name__
    # Exception type names are also allowlisted; no arbitrary object formatting.
    if kind not in {'OperationalError', 'DatabaseError', 'IntegrityError', 'ValueError',
                    'SourceError', 'TimeoutError', 'MemoryError', 'OSError', 'RuntimeError',
                    'InterruptedError', 'InterfaceError', 'PermissionError'}:
        kind = 'Exception'
    for error in chain(exc):
        if phase == 'local_store' and isinstance(error, sqlite3.Error):
            number = getattr(error, 'sqlite_errorcode', None)
            if type(number) is int and 0 <= number <= 65535:
                code = 'SQLITE_' + str(number)
                reason = SQLITE_CODES.get(number & 255, reason)
                break
        elif phase.startswith('source') or phase in ('connect', 'ultra_research_bar', 'ultra_research_universe'):
            args = error.args
            if isinstance(error, MySQLError) and args and type(args[0]) is int and 1000 <= args[0] <= 9999:
                code = 'MYSQL_' + str(args[0])
                reason = MYSQL_CODES.get(args[0], '源数据库返回错误，请按错误码检查数据库状态和读取权限')
                break
    if code == 'unknown':
        if isinstance(exc, TimeoutError):
            reason = '操作超时，请检查当前阶段对应的连接或存储状态后重试'
        elif isinstance(exc, MemoryError):
            reason = '可用内存不足，请检查服务器内存后重试'
        elif isinstance(exc, ValueError):
            reason = '数据或配置校验失败，请检查研究表字段、行情完整性、预热覆盖与日期范围'
            if exc.args and type(exc.args[0]) is str and exc.args[0] in SAFE_VALIDATIONS:
                reason = exc.args[0]
        elif phase == 'compute':
            reason = '信号计算失败，请保留任务并按任务标识检查服务器错误日志'
    frames = []
    for error in chain(exc):
        tb = error.__traceback__
        while tb is not None and len(frames) < 48:
            module = tb.tb_frame.f_globals.get('__name__', '')
            # Only static application/library module names, line numbers; never
            # source lines, paths, locals, exception args, or chained messages.
            if module in {'app.services.short_validation', 'app.services.short_statistics',
                          'app.services.validation_sources', 'app.services.validation_errors'}:
                location = module
            else:
                location = 'external'
            frames.append(f'{location}:{tb.tb_lineno}')
            tb = tb.tb_next
    return dict(phase=phase, kind=kind, code=code, reason=reason,
                trace='Traceback (safe locations only): ' + ' > '.join(frames))


def report(logger, exc, job=None, phase='worker'):
    info = diagnostic(exc, phase)
    ident = (job or {}).get('id', '')
    if not isinstance(ident, str) or len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
        ident = 'unassigned'
    logger.error('Validation failure job=%s phase=%s type=%s code=%s reason=%s\n%s',
                 ident, info['phase'], info['kind'], info['code'], info['reason'], info['trace'])
    advice = ('已保存数据保留，请新建任务。' if '新建任务' in info['reason'] or '创建新任务' in info['reason']
              else '已提交断点保留；修复后点击重试。')
    info['message'] = (f"{info['kind']}：{PHASES[info['phase']]}失败；{info['reason']}"
                       f"（错误码：{info['code']}；任务：{ident}）。{advice}")
    return info


SOURCE_PHASES = frozenset({'source_config', 'source_schema', 'source_plan', 'source_universe',
                           'source_read', 'connect', 'ultra_research_bar', 'ultra_research_universe'})
TRANSIENT_MYSQL = frozenset({1040, 1205, 1213, 2002, 2003, 2006, 2013, 2055})


def automatic_retry_allowed(exc, info):
    """Fail closed: an OperationalError name alone is not a retry policy."""
    phase, code = info['phase'], info['code']
    if phase == 'local_store':
        return code.startswith('SQLITE_') and int(code[7:]) & 255 in (5, 6)
    if phase not in SOURCE_PHASES:
        return False
    # Explicit validation failures must not inherit retryability from old context.
    if isinstance(exc, ValueError) and type(exc).__name__ != 'SourceError':
        return False
    import errno
    import socket
    import ssl
    errors = list(chain(exc))
    # PyMySQL wraps TLS/DNS OSErrors as 2003. Permanent inner errors veto
    # that broad outer code before any transient-code allowlist is consulted.
    for error in errors:
        if isinstance(error, (ssl.SSLError, PermissionError)):
            return False
        if isinstance(error, socket.gaierror) and error.errno != socket.EAI_AGAIN:
            return False
        if isinstance(error, MySQLError) and error.args and type(error.args[0]) is int:
            if error.args[0] not in TRANSIENT_MYSQL:
                return False
    if code.startswith('MYSQL_'):
        return int(code[6:]) in TRANSIENT_MYSQL
    temporary = {errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
                 errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE}
    for error in errors:
        if isinstance(error, ssl.SSLError):
            return False
        if isinstance(error, socket.gaierror):
            return error.errno == socket.EAI_AGAIN
        if isinstance(error, (TimeoutError, ConnectionResetError, ConnectionAbortedError,
                              ConnectionRefusedError, BrokenPipeError)):
            return True
        if isinstance(error, OSError) and error.errno in temporary:
            return True
        if getattr(error, '_validation_dns_temporary', False) is True:
            return True
    return False
