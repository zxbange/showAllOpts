# -*- coding: utf-8 -*-
"""七策略旧入口：保留命令行兼容，统计实现由程序共用。请保留完整项目目录。"""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / 'backend/app/services/short_statistics.py'
_spec = importlib.util.spec_from_file_location('short_statistics', _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
globals().update({name: value for name, value in vars(_module).items() if not name.startswith('__')})
if __name__ == '__main__':
    raise SystemExit(_module.main())
