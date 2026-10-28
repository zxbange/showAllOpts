"""聚宽目录懒加载与 Notebook 名称覆盖回归；不连接真实聚宽。"""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd


PATH = Path(__file__).resolve().parents[2] / '脚本/数据核对/聚宽两融担保比例字段核对.py'


class Query:
    def __init__(self, table):
        assert isinstance(table, type)
        self.table = table
        self.row_limit = None

    def limit(self, count):
        self.row_limit = count
        return self


class LazyDatabase:
    def __init__(self, warmup, table_name, extra=None):
        self.warmup, self.table_name = warmup, table_name
        self.calls, self.loaded = [], False
        self.extra = extra or {}

    def __getattr__(self, name):
        if name == self.warmup:
            self.loaded = True
            setattr(self, self.table_name, type(self.table_name, (), {}))
            for key, value in self.extra.items():
                setattr(self, key, value)
            return type(name, (), {})
        raise AttributeError(name)

    def load_names(self):
        raise AssertionError('Do not query interface methods as tables')

    def run_query(self, query):
        assert self.loaded and query.row_limit == 1
        self.calls.append(query.table.__name__)
        if query.table.__name__ == 'MT_Collateral':
            return pd.DataFrame([{'TradingDay': '2026-10-26', 'GuaranteeRatio': 2.556273}])
        return pd.DataFrame([{'date': '2026-10-26', 'fin_value': 123}])


def execute(monkeypatch, finance, jy):
    monkeypatch.setitem(sys.modules, 'jqdata', SimpleNamespace(finance=finance, jy=jy, query=Query))
    # Reproduce a research notebook polluted by `from numpy import *`.
    namespace = dict(__name__='__main__', any=np.any)
    exec(compile(PATH.read_text(encoding='utf-8'), str(PATH), 'exec'), namespace)


def test_lazy_catalog_with_numpy_any_and_method_names(monkeypatch, capsys):
    finance = LazyDatabase('STK_INCOME_STATEMENT', 'STK_MT_TOTAL', {'margin_helper': lambda: None})
    jy = LazyDatabase('LC_IncomeStatement', 'MT_TradingStat', {'MT_Collateral': type('MT_Collateral', (), {})})
    execute(monkeypatch, finance, jy)
    output = capsys.readouterr().out
    assert finance.calls == ['STK_MT_TOTAL']
    assert jy.calls == ['MT_Collateral', 'MT_TradingStat']
    assert 'margin_helper 跳过：不是数据表类' in output
    assert 'load_names' not in output and 'run_query' not in output
    assert output.count('候选字段样本') == 1
    assert '2.556273' in output and 'TradingDay' in output


def test_unavailable_tables_report_failure_not_absence(monkeypatch, capsys):
    execute(monkeypatch, SimpleNamespace(), SimpleNamespace())
    output = capsys.readouterr().out
    assert output.count('成功读取 0 张候选表') == 2
    assert 'STK_MT_TOTAL 无法读取：AttributeError' in output
    assert 'MT_TradingStat 无法读取：AttributeError' in output
    assert '不代表全库不存在' in output


def test_probe_queries_at_most_25_tables_per_database(monkeypatch, capsys):
    extra = {'MT_Test{:02d}'.format(i): type('MT_Test{:02d}'.format(i), (), {}) for i in range(30)}
    finance = LazyDatabase('STK_INCOME_STATEMENT', 'STK_MT_TOTAL', extra)
    jy = LazyDatabase('LC_IncomeStatement', 'MT_TradingStat')
    execute(monkeypatch, finance, jy)
    assert len(finance.calls) == 25 and len(jy.calls) == 1
    assert '仅检查前25张候选表' in capsys.readouterr().out


def test_probe_parses_on_python36():
    ast.parse(PATH.read_text(encoding='utf-8'), feature_version=(3, 6))
