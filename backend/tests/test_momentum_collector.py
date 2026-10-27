"""模拟聚宽行情，使用 SQLite 验证事务；不连接真实 MySQL。"""
import datetime
import builtins
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


PATH = Path(__file__).resolve().parents[2] / 'collectors/行业动量计算_数据库版.py'
jqdata = types.ModuleType('jqdata')
for name in ['get_all_securities', 'get_industries', 'get_industry_stocks',
             'get_price', 'get_trade_days']:
    setattr(jqdata, name, Mock())
spec = importlib.util.spec_from_file_location('momentum_database', PATH)
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'jqdata': jqdata}):
    spec.loader.exec_module(module)


class ResearchImportTest(unittest.TestCase):
    def test_builtin_research_interfaces_work_without_kuanke(self):
        partial_jqdata = types.ModuleType('jqdata')
        partial_jqdata.get_trade_days = Mock()
        partial_jqdata.datetime = object()
        names = ['get_all_securities', 'get_industries', 'get_industry_stocks', 'get_price']
        interfaces = {name: Mock() for name in names}
        imported = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'jqdata': partial_jqdata, 'kuanke': None}), \
                patch.multiple(builtins, create=True, **interfaces):
            spec.loader.exec_module(imported)
            for name in names:
                self.assertNotIn(name, imported.__dict__)
                # 与程序中的普通函数调用一样，Python 会继续查找内置命名空间。
                self.assertIs(eval(name, imported.__dict__), interfaces[name])
            interfaces['get_industries'].return_value = pd.DataFrame()
            with self.assertRaisesRegex(ValueError, '未获取到行业列表'):
                imported.calculate_momentum('2026-03-31')
            interfaces['get_industries'].assert_called_once_with(
                name='sw_l2', date='2026-03-31')
        self.assertIs(imported.get_trade_days, partial_jqdata.get_trade_days)
        self.assertIs(imported.datetime, datetime)

    def test_notebook_existing_interfaces_are_preserved(self):
        partial_jqdata = types.ModuleType('jqdata')
        partial_jqdata.get_trade_days = Mock()
        imported = importlib.util.module_from_spec(spec)
        names = ['get_all_securities', 'get_industries', 'get_industry_stocks', 'get_price']
        existing = {name: Mock() for name in names}
        imported.__dict__.update(existing)
        with patch.dict(sys.modules, {'jqdata': partial_jqdata, 'kuanke': None}):
            spec.loader.exec_module(imported)
        for name in names:
            self.assertIs(getattr(imported, name), existing[name])


class MomentumDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.day = datetime.date(2026, 3, 31)
        self.days = [d.date() for d in pd.bdate_range(end=self.day, periods=21)]
        self.members = {'A': ['a', 'b', 'exact', 'new', 'dead'],
                        'B': ['flat', 'missing'], 'C': []}
        self.industries = pd.DataFrame({'name': ['行业甲II', '行业乙II', '空行业']},
                                       index=['A', 'B', 'C'])
        self.securities = pd.DataFrame(
            {'start_date': [datetime.date(2020, 1, 1)] * 7,
             'end_date': [datetime.date(2200, 1, 1)] * 7},
            index=['a', 'b', 'exact', 'new', 'dead', 'flat', 'missing'])
        self.securities.loc['new', 'start_date'] = self.day - datetime.timedelta(days=10)
        self.securities.loc['dead', 'end_date'] = self.day
        rows = []
        for code, last in [('a', 13), ('b', 14), ('exact', 12), ('flat', 10)]:
            rows.extend({'code': code, 'time': d, 'close': last if d == self.day else 10}
                        for d in self.days)
        rows.append({'code': 'missing', 'time': self.day, 'close': 30})
        # 乱序数据必须仍按日期端点计算。
        self.prices = pd.DataFrame(rows[::-1])
        self.patches = [
            patch.object(module, 'get_industries', return_value=self.industries),
            patch.object(module, 'get_industry_stocks', side_effect=lambda code, date: self.members[code]),
            patch.object(module, 'get_all_securities', return_value=self.securities),
            patch.object(module, 'get_trade_days', return_value=self.days),
            patch.object(module, 'get_price', return_value=self.prices),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_engine('sqlite:///:memory:')
        self.addCleanup(self.engine.dispose)
        module.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def rows(self, day=None):
        session = self.Session()
        try:
            return session.query(module.IndustryMomentum).filter_by(
                trade_date=day or self.day).order_by(module.IndustryMomentum.momentum_rank).all()
        finally:
            session.close()

    def test_formula_all_industries_zero_scores_and_historical_dates(self):
        records = module.calculate_momentum(self.day)
        self.assertEqual([r['industry_code'] for r in records], ['A', 'C', 'B'])
        self.assertEqual([float(r['momentum']) for r in records], [.8, 0, 0])
        self.assertEqual(records[0]['front_stock_count'], 2)
        self.assertEqual(records[0]['industry_stock_count'], 5)
        self.assertEqual([r['momentum_rank'] for r in records], [1, 2, 3])
        module.get_industries.assert_called_once_with(name='sw_l2', date='2026-03-31')
        module.get_all_securities.assert_called_once_with(types=['stock'], date='2026-03-31')
        self.assertEqual(module.get_industry_stocks.call_count, 3)
        for call in module.get_industry_stocks.call_args_list:
            self.assertEqual(call.kwargs['date'], '2026-03-31')
        eligible = module.get_price.call_args.args[0]
        self.assertNotIn('new', eligible)
        self.assertNotIn('dead', eligible)

    def test_valid_all_zero_day_is_saved(self):
        self.prices['close'] = 10
        records = module.calculate_momentum(self.day)
        module.save_day(self.Session, self.day, 'sw_l2', records)
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(r.momentum == 0 for r in self.rows()))

    def test_rerun_replaces_snapshot_and_preserves_other_dates(self):
        records = module.calculate_momentum(self.day)
        previous = self.day - datetime.timedelta(days=1)
        old_records = [dict(r, trade_date=previous) for r in records]
        module.save_day(self.Session, previous, 'sw_l2', old_records)
        module.save_day(self.Session, self.day, 'sw_l2', records)
        module.save_day(self.Session, self.day, 'sw_l2', records)
        self.assertEqual(len(self.rows()), 3)
        changed = [dict(records[0], industry_name='更新后的名称', momentum=2)]
        module.save_day(self.Session, self.day, 'sw_l2', changed)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].industry_name, '更新后的名称')
        self.assertEqual(self.rows()[0].momentum, 2)
        self.assertEqual(len(self.rows(previous)), 3)

    def test_insert_failure_rolls_back_deleted_snapshot(self):
        records = module.calculate_momentum(self.day)
        module.save_day(self.Session, self.day, 'sw_l2', records)
        with self.assertRaises(IntegrityError):
            module.save_day(self.Session, self.day, 'sw_l2', [records[0], records[0]])
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(float(self.rows()[0].momentum), .8)

    def test_missing_data_does_not_become_zero_day(self):
        module.get_price.return_value = pd.DataFrame()
        with self.assertRaisesRegex(ValueError, '行情为空'):
            module.calculate_momentum(self.day)
        module.get_price.return_value = self.prices[self.prices['code'] == 'missing']
        with self.assertRaisesRegex(ValueError, '没有可计算涨幅'):
            module.calculate_momentum(self.day)

    def test_empty_or_mismatched_snapshot_cannot_delete_existing_data(self):
        records = module.calculate_momentum(self.day)
        module.save_day(self.Session, self.day, 'sw_l2', records)
        for bad in [[], [dict(records[0], industry_type='sw_l1')]]:
            with self.assertRaises(ValueError):
                module.save_day(self.Session, self.day, 'sw_l2', bad)
        self.assertEqual(len(self.rows()), 3)

    def test_numpy_any_in_notebook_does_not_reject_valid_snapshot(self):
        records = module.calculate_momentum(self.day)
        with patch.object(module, 'any', np.any, create=True):
            module.save_day(self.Session, self.day, 'sw_l2', records)
            self.assertEqual(len(self.rows()), 3)
            for invalid in [dict(records[0], industry_type='sw_l1'),
                            dict(records[0], trade_date=self.day - datetime.timedelta(days=1))]:
                with self.assertRaisesRegex(ValueError, '预期日期=.*实际日期='):
                    module.save_day(self.Session, self.day, 'sw_l2', [invalid])
            self.assertEqual(len(self.rows()), 3)
