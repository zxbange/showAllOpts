"""Manually synchronize local snapshots without starting the web server."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app.core.cache import MODULES, SnapshotCache
from app.core.config import Settings
from app.core.database import build_engine
from app.core.logging import configure_logging


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', choices=MODULES, help='Refresh one module; default: all modules')
    parser.add_argument('--status', action='store_true', help='Show local status without querying MySQL')
    parser.add_argument('--env-file', help='Explicit app configuration path, relative to project root')
    options = parser.parse_args(argv)
    if options.env_file:
        os.environ['APP_ENV_FILE'] = options.env_file
    elif sys.platform.startswith('linux'):
        os.environ.setdefault('APP_ENV_FILE', 'config/app.config')
    source = None
    try:
        settings = Settings()
        configure_logging(settings)
        source = build_engine(settings)
        cache = SnapshotCache(settings, source)
        if options.status:
            print(json.dumps(cache.status(), ensure_ascii=False, indent=2))
            return 0
        if not cache.refresh(options.module, background=False, reason='cli'):
            print('已有同步任务正在执行；本次未重复启动。')
            return 2
        result = cache.status()
        selected = [m for m in result['modules'] if options.module is None or m['id'] == options.module]
        for entry in selected:
            print('{}: {}，记录数={}，上次成功同步={}'.format(
                entry['name'], entry['error'] or '同步成功', entry['rows'], entry['updated_at']))
        return 1 if any(m['error'] or not m['ready'] for m in selected) else 0
    except Exception as exc:
        logging.getLogger(__name__).error('Cache command failed: %s; check configuration, database and cache directory permissions.', type(exc).__name__)
        return 1
    finally:
        if source is not None:
            source.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
