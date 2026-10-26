"""Preview or repair saved institution classifications without querying JoinQuant.

Examples:
  python scripts/repair_ownership_classification.py --code 002281
  python scripts/repair_ownership_classification.py --apply
The default is read-only; --apply updates only changed reports and their sync journal.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker
from app.core.config import Settings
from app.core.database import build_engine


def stock_code(value):
    value = value.strip().upper()
    if not re.fullmatch(r'[0-9]{6}(?:\.(?:XSHG|XSHE|XBSE))?', value):
        raise argparse.ArgumentTypeError('股票代码须为六位数字或带 XSHG/XSHE/XBSE 后缀。')
    return value


def load_collector():
    """Load definitions only, restoring any existing jqdata module afterwards."""
    sentinel = object()
    previous = sys.modules.get('jqdata', sentinel)
    sys.modules['jqdata'] = ModuleType('jqdata')
    try:
        spec = importlib.util.spec_from_file_location(
            'ownership_classification_repair', ROOT / 'collectors' / '个股机构持仓_数据库版.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is sentinel:
            sys.modules.pop('jqdata', None)
        else:
            sys.modules['jqdata'] = previous
    module.PROGRESS_ENABLED = False
    module._progress_note = lambda *args, **kwargs: None
    module._progress_message = lambda *args, **kwargs: None
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='写入修复结果；默认仅预览')
    parser.add_argument('--code', action='append', type=stock_code, help='限定股票，可重复；默认检查旧的不完整记录')
    parser.add_argument('--env-file', help='配置文件路径，相对项目根目录')
    options = parser.parse_args(argv)
    if options.env_file:
        os.environ['APP_ENV_FILE'] = options.env_file
    elif sys.platform.startswith('linux'):
        os.environ.setdefault('APP_ENV_FILE', 'config/app.config')
    engine = None
    try:
        settings = Settings()
        engine = build_engine(settings, for_cache=True)
        required = ('ownership_stock', 'ownership_report', 'ownership_disclosure_probe',
                    'ownership_sync_head', 'ownership_change')
        inspector = inspect(engine)
        missing = [name for name in required if not inspector.has_table(name)]
        if missing:
            print('缺少已有机构持仓表：{}；请先运行新版采集脚本。'.format(', '.join(missing)))
            return 2
        module = load_collector()
        ns = module._ownership_strategy({'finance': None, 'query': None}, module.DB_CONFIG)
        Session = sessionmaker(bind=engine)
        as_of = module.china_now().date()
        codes = None
        with Session() as session:
            probe = session.query(ns.DisclosureProbe).filter_by(key='main').first()
            if probe is not None and probe.as_of > as_of:
                print('修复日期早于最新采集日期，已停止；请核对系统北京时间。')
                return 2
            if options.code:
                codes = []
                for code in options.code:
                    query = session.query(ns.OwnershipStock.code)
                    query = query.filter(ns.OwnershipStock.code == code) if '.' in code else query.filter(
                        ns.OwnershipStock.code.startswith(code + '.'))
                    matches = [row[0] for row in query.all()]
                    if len(matches) != 1:
                        print('股票代码 {} 未找到唯一已有档案，已停止。'.format(code))
                        return 2
                    codes.append(matches[0])
                codes = sorted(set(codes))
        if options.apply:
            ns.install_change_tracking(Session)
        changed = set()
        count = ns.repair_classifications(Session, as_of, changed=changed,
                                          codes=codes, dry_run=not options.apply)
        print('{}：{} 份报告，涉及 {} 只股票。'.format(
            '已修复' if options.apply else '只读预览，待修复', count, len({item[1] for item in changed})))
        for _, code, period in sorted(changed)[:50]:
            print('{} {}'.format(code, period))
        if len(changed) > 50:
            print('其余 {} 份报告未逐条展开。'.format(len(changed) - 50))
        return 0
    except Exception as exc:
        args = getattr(getattr(exc, 'orig', None), 'args', ())
        code = args[0] if args and isinstance(args[0], int) else None
        print('修复失败：{}{}。'.format(type(exc).__name__, ' (数据库代码 {})'.format(code) if code is not None else ''))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())