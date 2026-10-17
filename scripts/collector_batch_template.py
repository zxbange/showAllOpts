# -*- coding: utf-8 -*-
"""聚宽单文件串行采集入口。由 scripts/build_collector_batch.py 生成。
Notebook 中粘贴运行，或 %run -i 策略批量采集_数据库版.py。
仅在私有副本填写 DB_CONFIG；完整用法见 docs/collector-batch.md。
"""
import builtins as _batch_builtins
import datetime as _batch_datetime
import io as _batch_io
import os as _batch_os
import re as _batch_re
import sys as _batch_sys
import time as _batch_time
import traceback as _batch_traceback
import uuid as _batch_uuid
from contextlib import redirect_stdout as _batch_stdout, redirect_stderr as _batch_stderr
from pathlib import Path as _BatchPath
from urllib.parse import quote as _batch_quote, quote_plus as _batch_quote_plus

DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}
# 按此顺序运行；临时只跑部分策略时，可删除不需要的名称。
STRATEGY_ORDER = ['行业动量', '抱团拥挤度', '微盘温度计', 'ETF份额', '净利润断层']
LOG_DIR = 'log'  # 相对于聚宽研究环境当前工作目录；按北京时间日期生成日志文件。
# 可选：传给各策略 run() 的参数；空字典表示保持各策略原有默认值。
STRATEGY_OPTIONS = {}

_STRATEGY_CLASSES = {
    '行业动量': 'MysqlMomentum', '抱团拥挤度': 'MysqlCrowding',
    '微盘温度计': 'MysqlMicrocap', 'ETF份额': 'MysqlETF', '净利润断层': 'MysqlEarnings',
}


def _batch_now():
    return _batch_datetime.datetime.utcnow() + _batch_datetime.timedelta(hours=8)


class BatchRunError(RuntimeError):
    """所有选中策略执行完毕后报告失败；summary 保存逐策略结果。"""
    def __init__(self, summary):
        self.summary = summary
        failed = [row['strategy'] for row in summary if row['status'] == 'FAILED']
        super(BatchRunError, self).__init__('批量采集存在失败策略：{}。请查看日志；成功策略已提交。'.format('、'.join(failed)))


class _BatchLog:
    def __init__(self, directory, config):
        self.directory = _BatchPath(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.console = _batch_sys.stdout
        self.run_id = _batch_now().strftime('%Y%m%d-%H%M%S') + '-' + _batch_uuid.uuid4().hex[:8]
        self.file = None
        self.day = None
        self.secrets = set()
        for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
            value = str(config.get(key, ''))
            if value:
                self.secrets.update((value, _batch_quote(value, safe=''), _batch_quote_plus(value)))
        self.secrets = sorted(self.secrets, key=len, reverse=True)
        self._open_day(_batch_now().date())  # 写日志失败时，不开始任何策略或数据库操作。

    def _open_day(self, day):
        if self.day == day and self.file is not None:
            return
        if self.file is not None:
            self.file.close()
        path = self.directory / ('collector-batch-{}.log'.format(day.isoformat()))
        self.file = path.open('a', encoding='utf-8')
        self.day = day

    def redact(self, value):
        message = str(value)
        message = _batch_re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', message)
        message = _batch_re.sub(
            r'''(?i)(\b(?:password|passwd|pwd|token|secret|authorization)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s&,;\)\]\}]+)''',
            r'\1[REDACTED]', message)
        for secret in self.secrets:
            message = message.replace(secret, '[REDACTED]')
        return message

    def write(self, level, strategy, message):
        now = _batch_now()
        self._open_day(now.date())
        prefix = '{}+08:00 | {:<7} | run={} | {} | '.format(
            now.isoformat(timespec='milliseconds'), level, self.run_id, strategy)
        lines = self.redact(message).replace('\r', '\n').splitlines()
        for line in lines:
            if not line.strip():
                continue
            text = prefix + line + '\n'
            self.file.write(text)
            self.file.flush()
            self.console.write(text)
            self.console.flush()

    def close(self):
        if self.file is not None:
            self.file.close()


class _BatchStream(_batch_io.TextIOBase):
    def __init__(self, log, strategy, level):
        self.log, self.strategy, self.level = log, strategy, level
        self.pending = ''

    @property
    def encoding(self):
        return 'utf-8'

    def writable(self):
        return True

    def write(self, text):
        self.pending += text.replace('\r', '\n')
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            self.log.write(self.level, self.strategy, line)
        return len(text)

    def flush(self):
        if self.pending:
            pending, self.pending = self.pending, ''
            self.log.write(self.level, self.strategy, pending)


class _BatchLock:
    """同一 log 目录下互斥运行，进程退出自动释放，不阻塞等待。"""
    def __init__(self, directory):
        self.path = _BatchPath(directory).expanduser().resolve() / '.collector-batch.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if _batch_os.name == 'nt':
                import msvcrt
                self.file.seek(0, 2)
                if self.file.tell() == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('无法取得批量采集锁；同一日志目录可能已有任务运行，请等待完成并检查文件系统权限。') from None
        return self

    def __exit__(self, *args):
        try:
            if _batch_os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


def _load_strategy(name, config, api_namespace):
    # 独立全局字典隔离 Base、run、日期常量等；沿用 Notebook 提供的聚宽 API。
    namespace = dict(api_namespace)
    namespace['__name__'] = '_batch_collector_' + _STRATEGY_CLASSES[name]
    namespace['__file__'] = name + '_数据库版.py'
    exec(compile(_SOURCES[name], namespace['__file__'], 'exec'), namespace)
    connector = namespace[_STRATEGY_CLASSES[name]]
    for key, value in config.items():
        setattr(connector, key, value)
    return namespace['run']


def run_all(strategy_names=None, options=None, db_config=None, log_dir=None,
            raise_on_failure=True, api_namespace=None):
    """串行执行，单策略失败后继续；结束时汇总，并默认抛出批次失败异常。"""
    names = list(STRATEGY_ORDER if strategy_names is None else strategy_names)
    if not names or len(names) != len(set(names)) or _batch_builtins.any(name not in _STRATEGY_CLASSES for name in names):
        raise ValueError('策略列表不能为空、重复或包含未知名称。')
    config = dict(DB_CONFIG if db_config is None else db_config)
    if set(config) != {'host', 'port', 'dbname', 'dbuser', 'dbpassword'}:
        raise ValueError('DB_CONFIG 必须包含 host、port、dbname、dbuser、dbpassword。')
    selected_options = dict(STRATEGY_OPTIONS if options is None else options)
    if _batch_builtins.any(name not in _STRATEGY_CLASSES or not isinstance(value, dict) for name, value in selected_options.items()):
        raise ValueError('策略参数必须为按策略名称组织的字典。')
    api_namespace = dict(globals() if api_namespace is None else api_namespace)
    directory = LOG_DIR if log_dir is None else log_dir
    summary = []
    with _BatchLock(directory):
        log = _BatchLog(directory, config)
        batch_start = _batch_time.monotonic()
        try:
            log.write('INFO', '批次', '开始串行采集，共 {} 个策略；日志目录：{}'.format(len(names), log.directory))
            for index, name in enumerate(names, 1):
                started = _batch_time.monotonic()
                row = dict(strategy=name, status='RUNNING', rows=None, elapsed_seconds=0.0, error_type=None)
                log.write('INFO', name, '========== [{}/{}] 开始 =========='.format(index, len(names)))
                stdout = _BatchStream(log, name, 'INFO')
                stderr = _BatchStream(log, name, 'WARNING')
                try:
                    with _batch_stdout(stdout), _batch_stderr(stderr):
                        run = _load_strategy(name, config, api_namespace)
                        result = run(**selected_options.get(name, {}))
                        count = len(result)
                        del result  # 不累积所有策略的行情/历史结果，减少 Notebook 内存占用。
                    row.update(status='SUCCESS', rows=count)
                except KeyboardInterrupt:
                    log.write('WARNING', name, '用户中断，停止整个批次；已提交数据保留。')
                    raise
                except Exception as exc:
                    stdout.flush()
                    stderr.flush()
                    row.update(status='FAILED', error_type=type(exc).__name__)
                    # 不输出源代码行或局部变量；驱动消息先脱敏再写日志。
                    frames = ['{}:{} in {}'.format(frame.filename, frame.lineno, frame.name)
                              for frame in _batch_traceback.extract_tb(exc.__traceback__)]
                    log.write('ERROR', name, '{}: {}\n{}'.format(type(exc).__name__, exc, '\n'.join(frames)))
                finally:
                    stdout.flush()
                    stderr.flush()
                    # TextIOBase 对象析构可能发生在日志关闭之后，此处清空后不再持有。
                    stdout.close()
                    stderr.close()
                row['elapsed_seconds'] = _batch_builtins.round(_batch_time.monotonic() - started, 3)
                summary.append(row)
                if row['status'] == 'SUCCESS':
                    log.write('INFO', name, '========== 成功 | 本次处理/写入 {} 条（含覆盖） | 耗时 {:.2f} 秒 =========='.format(row['rows'], row['elapsed_seconds']))
                else:
                    log.write('ERROR', name, '========== 失败 | 耗时 {:.2f} 秒 | 已提交部分保留，继续后续策略 =========='.format(row['elapsed_seconds']))
            successes = _batch_builtins.sum(row['status'] == 'SUCCESS' for row in summary)
            log.write('INFO', '汇总', '策略 | 状态 | 本次处理/写入条数（含覆盖） | 耗时秒')
            for row in summary:
                log.write('INFO' if row['status'] == 'SUCCESS' else 'ERROR', '汇总', '{} | {} | {} | {:.2f}'.format(
                    row['strategy'], row['status'], row['rows'] if row['rows'] is not None else '未完成，详见过程日志', row['elapsed_seconds']))
            log.write('INFO', '批次', '执行完成：成功 {}，失败 {}，总耗时 {:.2f} 秒。'.format(
                successes, len(summary) - successes, _batch_time.monotonic() - batch_start))
            if successes != len(summary) and raise_on_failure:
                raise BatchRunError(summary)
            return summary
        finally:
            log.close()


# 以下嵌入独立策略源码，无需在聚宽额外上传或安装其他项目文件。
_SOURCES = {}  # GENERATED_SOURCES

if __name__ == '__main__':
    batch_summary = run_all()
