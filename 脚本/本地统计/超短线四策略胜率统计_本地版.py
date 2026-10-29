# -*- coding: utf-8 -*-
"""兼容旧命令：统一实现位于同目录七策略胜率统计_本地版.py；配置路径不变。"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('seven_strategy_statistics',
    Path(__file__).resolve().with_name('七策略胜率统计_本地版.py'))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

if __name__ == '__main__':
    raise SystemExit(_module.main())
