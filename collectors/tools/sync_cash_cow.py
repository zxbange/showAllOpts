"""Keep the website, independent JoinQuant entry and batch on one screening rule."""
import ast
from pathlib import Path
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[2]


def write(path, content, check):
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return
    if check:
        raise SystemExit('奶牛股内嵌规则未同步：' + str(path.relative_to(ROOT)))
    path.write_text(content, encoding='utf-8')


def sync(check=False):
    rules = (ROOT / 'collectors/rules/cash_cow.py').read_text(encoding='utf-8')
    rules = rules[rules.index('import math'):]
    write(ROOT / 'backend/app/services/cash_cow_rules.py',
          '"""Generated from collectors/rules/cash_cow.py; use sync_cash_cow.py."""\n' + rules, check)
    rules = rules.removeprefix('import math\n\n')
    template = (ROOT / 'collectors/rules/cash_cow_strategy.py').read_text(encoding='utf-8')
    factory = template[template.index('def _cash_cow_strategy'):]
    factory = factory.replace('    # BEGIN CASH COW RULES\n    # END CASH COW RULES',
        '    # BEGIN CASH COW RULES\n' + textwrap.indent(rules.rstrip(), '    ') + '\n    # END CASH COW RULES')
    batch_path = ROOT / 'collectors/策略批量采集_数据库版.py'
    batch = batch_path.read_text(encoding='utf-8')
    start, end = '# BEGIN EMBEDDED CASH COW', '# END EMBEDDED CASH COW'
    block = start + '\n' + factory.rstrip() + '\n' + end
    if start in batch:
        batch = batch[:batch.index(start)] + block + batch[batch.index(end) + len(end):]
    else:
        at = batch.index('# 注册信息：')
        batch = batch[:at] + block + '\n\n' + batch[at:]
    write(batch_path, batch, check)
    nodes = {node.name: node for node in ast.parse(batch).body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    lines = batch.splitlines()
    names = ('_ConsoleProgress', '_progress_scope', '_progress_note', '_progress_message',
             'strategy_settings', 'china_now', 'as_date', '_float_number', '_optional_date', '_closed_day',
             '_checked_records', '_source_pages', 'connect_database', 'session_scope', '_safe_error')
    helpers = []
    for name in names:
        node = nodes[name]
        begin = min([node.lineno] + [d.lineno for d in node.decorator_list])
        helpers.append('\n'.join(lines[begin-1:node.end_lineno]))
    header = '''# -*- coding: utf-8 -*-
"""聚宽研究环境：奶牛股近3/5/10年筛选、静态股息率及三年分红展示。
与 mysql_connector.py 一起上传后，完整运行本文件即计算并写库。
数据库配置仅在 mysql_connector.py；不调用其他策略脚本。
只读取候选股的历史日线；当日完整快照已存在则跳过，三组结果原子发布。
默认最近收市交易日；run(end_date='2025-12-31', write_database=False) 可历史复算预览。
复算使用当前数据库的财报修订值，不声称还原当时原始披露版本。
运行后在网站同步“奶牛股”；原东方财富采集保持可用。详见 docs/data/cash-cow.md。
"""
import builtins
import datetime
import gc
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from builtins import all, any, bool, float, int, isinstance, len, max, min, range, set, sorted, str, sum, type
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote, quote_plus
import jqdata as jq
import pandas as pd
from sqlalchemy import Column, Date, DateTime, Integer, JSON, create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from mysql_connector import get_database_config, get_database_options, mysql_drivername, engine_options

DB_CONFIG = get_database_config()
DB_OPTIONS = get_database_options()
STRATEGY_CONFIG = {'奶牛股': {'end_date': None, 'batch_size': 300}}
PROGRESS_ENABLED = True
PROGRESS_INTERVAL_SECONDS = 5.0
_PROGRESS_LOCAL = threading.local()
'''
    footer = '''
_API_NAMES = ('get_all_securities', 'get_trade_days', 'get_price', 'get_fundamentals',
              'query', 'indicator', 'income', 'cash_flow', 'finance')


def run(database_config=None, **options):
    apis = {key: globals().get(key, getattr(jq, key, getattr(builtins, key, None))) for key in _API_NAMES}
    missing = [key for key, value in apis.items() if value is None]
    if missing:
        raise RuntimeError('聚宽环境缺少接口：' + ', '.join(missing))
    with _progress_scope('奶牛股', 1, 1):
        return _cash_cow_strategy(apis, DB_CONFIG if database_config is None else dict(database_config)).run(**options)


if __name__ == '__main__':
    try:
        cash_cow_df = run()
        print('奶牛股采集完成：3/5/10年合计 {} 条；请同步网站奶牛股数据。'.format(len(cash_cow_df)))
        if len(cash_cow_df):
            print(cash_cow_df.to_string(index=False))
    except Exception as exc:
        raise RuntimeError(_safe_error(exc, DB_CONFIG)) from None
'''
    write(ROOT / 'collectors/奶牛股选股_数据库版.py', header + '\n\n' + '\n\n\n'.join(helpers)
          + '\n\n\n' + factory.rstrip() + '\n\n' + footer, check)


if __name__ == '__main__':
    sync('--check' in sys.argv)
