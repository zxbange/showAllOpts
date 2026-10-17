# -*- coding: utf-8 -*-
"""聚宽模块化串行采集入口；将本文件和五个策略文件放在同一目录。
Notebook 中执行 %run -i 策略批量采集_数据库版.py，详见 docs/collector-batch.md。
"""
import builtins as _b
import importlib.util as _import_util
import io as _io
import os as _batch_os
import re as _re
import time as _time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path as _BatchPath
from urllib.parse import quote, quote_plus

DB_CONFIG = {
    'host': 'your_mysql_host',
    'port': 3306,
    'dbname': 'your_database',
    'dbuser': 'your_database_user',
    'dbpassword': 'your_password',
}
STRATEGIES = {
    '行业动量': ('行业动量计算_数据库版.py', 'MysqlMomentum'),
    '抱团拥挤度': ('抱团拥挤度_数据库版.py', 'MysqlCrowding'),
    '微盘温度计': ('微盘温度计_数据库版.py', 'MysqlMicrocap'),
    'ETF份额': ('ETF份额_数据库版.py', 'MysqlETF'),
    '净利润断层': ('净利润断层_数据库版.py', 'MysqlEarnings'),
}
STRATEGY_ORDER = ['行业动量', '抱团拥挤度', '微盘温度计', 'ETF份额', '净利润断层']
STRATEGY_OPTIONS = {}  # 按策略名称传入 run() 参数；固定 skip_existing=True。
PREVIEW_ROWS = 5  # 每个策略展示最后几条新增记录；0 表示仅展示条数。
_MODULE_DIR = _BatchPath(__file__).resolve().parent
_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_extras',
              'get_industries', 'get_industry_stocks', 'get_industry', 'get_fundamentals',
              'query', 'valuation', 'indicator', 'income', 'finance', 'jy')
_PREVIEW_COLUMNS = {
    '行业动量': ('trade_date', 'industry_name', 'momentum', 'momentum_rank'),
    '抱团拥挤度': ('trade_date', 'top_pct', 'ratio', 'top_money', 'total_money'),
    '微盘温度计': ('trade_date', 'score', 'micro_index', 'sample_count'),
    'ETF份额': ('trade_date', 'fund_code', 'fund_name', 'shares'),
    '净利润断层': ('report_period', 'code', 'name', 'industry_name', 'signal_date', 'profit_yoy'),
}


class BatchRunError(RuntimeError):
    def __init__(self, summary):
        self.summary = summary
        failed = [r['strategy'] for r in summary if r['status'] == 'FAILED']
        super(BatchRunError, self).__init__('采集未全部完成：{}；请查看控制台结果。'.format('、'.join(failed)))


class _QuietStream(_io.TextIOBase):
    """丢弃策略过程输出，不在内存累积历史日志，也不创建日志文件。"""
    def write(self, text):
        return len(text)

    def flush(self):
        pass


class _BatchLock:
    """同一模块目录下互斥运行，进程退出自动释放，不阻塞等待。"""
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
            raise RuntimeError('无法取得批量采集锁；同一模块目录可能已有任务运行，请等待完成并检查文件系统权限。') from None
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
    filename, class_name = STRATEGIES[name]
    # 每次新建模块，避免 Notebook 重跑沿用旧参数或各策略的同名变量互相覆盖。
    spec = _import_util.spec_from_file_location('_collector_' + class_name, str(_MODULE_DIR / filename))
    module = _import_util.module_from_spec(spec)
    for key in _API_NAMES:
        if key in api_namespace:
            setattr(module, key, api_namespace[key])
    spec.loader.exec_module(module)
    connector = getattr(module, class_name)
    for key, value in config.items():
        setattr(connector, key, value)
    return module.run


def _safe_error(exc, config):
    message = _re.sub(r'(?i)[a-z][a-z0-9+.-]*://[^\s]+', '[连接地址已隐藏]', str(exc))
    message = _re.sub(r"(?i)(password|passwd|pwd|token|secret)\s*[:=]\s*('.*?'|\".*?\"|[^\s,;]+)",
                      r'\1=[已隐藏]', message)
    secrets = set()
    for key in ('host', 'dbname', 'dbuser', 'dbpassword'):
        value = str(config[key])
        if value:
            secrets.update((value, quote(value, safe=''), quote_plus(value)))
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, '[已隐藏]')
    return '{}: {}'.format(type(exc).__name__, ' '.join(message.split())[:400])


def run_all(strategy_names=None, options=None, db_config=None, preview_rows=None,
            raise_on_failure=True, api_namespace=None):
    """依次加载策略，只新增；控制台展示结果，失败后继续，最后统一报告。"""
    names = list(STRATEGY_ORDER if strategy_names is None else strategy_names)
    config = dict(DB_CONFIG if db_config is None else db_config)
    options = dict(STRATEGY_OPTIONS if options is None else options)
    preview_rows = PREVIEW_ROWS if preview_rows is None else preview_rows
    if not names or len(names) != len(set(names)) or _b.any(n not in STRATEGIES for n in names):
        raise ValueError('策略列表不能为空、重复或包含未知名称。')
    if set(config) != {'host', 'port', 'dbname', 'dbuser', 'dbpassword'}:
        raise ValueError('DB_CONFIG 必须包含 host、port、dbname、dbuser、dbpassword。')
    if _b.any(n not in STRATEGIES or not isinstance(v, dict) for n, v in options.items()):
        raise ValueError('策略参数必须为按策略名称组织的字典。')
    if _b.any(v.get('skip_existing', True) is not True for v in options.values()):
        raise ValueError('不允许关闭 skip_existing；已有股票及其他明细保持原值。')
    if not isinstance(preview_rows, int) or preview_rows < 0:
        raise ValueError('preview_rows 必须是非负整数。')
    apis = globals() if api_namespace is None else api_namespace
    summary = []
    with _BatchLock(_MODULE_DIR):
        for name in names:
            started = _time.monotonic()
            row = dict(strategy=name, status='FAILED', rows=None, error_type=None, error=None)
            result = None
            try:
                with _QuietStream() as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    run = _load_strategy(name, config, apis)
                    kwargs = dict(options.get(name, {}), skip_existing=True)
                    result = run(**kwargs)
                    row.update(status='SUCCESS', rows=len(result))
            except KeyboardInterrupt:
                print('【{}】已中断；已提交数据保留，后续策略不再执行。'.format(name))
                raise
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=_safe_error(exc, config))
            row['elapsed_seconds'] = _b.round(_time.monotonic() - started, 3)
            summary.append(row)
            if row['status'] == 'SUCCESS':
                print('\n【{}】成功：新增 {} 条，耗时 {:.2f} 秒。'.format(name, row['rows'], row['elapsed_seconds']))
                if not row['rows']:
                    print('无新增数据；已有记录跳过，或数据源尚无可入库数据。')
                elif preview_rows:
                    try:
                        columns = [c for c in _PREVIEW_COLUMNS[name] if c in result.columns]
                        print('本次新增结果（最后 {} 条，字段单位沿用策略）：'.format(_b.min(preview_rows, row['rows'])))
                        print(result[columns].tail(preview_rows).to_string(index=False))
                    except Exception:
                        print('结果预览不可用，入库已完成。')
            else:
                print('\n【{}】失败：{}；已提交部分保留，继续后续策略。'.format(name, row['error']))
            del result
        print('\n采集结果汇总\n策略 | 结果 | 新增条数 | 耗时（秒）')
        for row in summary:
            print('{} | {} | {} | {:.2f}'.format(row['strategy'], row['status'],
                  row['rows'] if row['rows'] is not None else '未完成', row['elapsed_seconds']))
        failed = _b.sum(r['status'] == 'FAILED' for r in summary)
        print('成功 {}，失败 {}。'.format(len(summary) - failed, failed))
        if failed and raise_on_failure:
            raise BatchRunError(summary)
    return summary


if __name__ == '__main__':
    batch_summary = run_all()
