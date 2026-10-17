"""Build/check the self-contained JoinQuant batch collector from public sources."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = [
    ('行业动量', '行业动量计算_数据库版.py'),
    ('抱团拥挤度', '抱团拥挤度_数据库版.py'),
    ('微盘温度计', '微盘温度计_数据库版.py'),
    ('ETF份额', 'ETF份额_数据库版.py'),
    ('净利润断层', '净利润断层_数据库版.py'),
]
TARGET = ROOT / 'collectors' / '策略批量采集_数据库版.py'


def render():
    template = (ROOT / 'scripts' / 'collector_batch_template.py').read_text(encoding='utf-8')
    parts = []
    for name, filename in STRATEGIES:
        source = (ROOT / 'collectors' / filename).read_text(encoding='utf-8')
        escaped = source.replace('\\', '\\\\').replace('"""', '\\"\\"\\"')
        parts.append('    {!r}: """{}""",'.format(name, escaped))
    sources = '_SOURCES = {\n' + '\n'.join(parts) + '\n}'
    return template.replace('_SOURCES = {}  # GENERATED_SOURCES', sources)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding='utf-8') != expected:
            raise SystemExit('Batch collector is stale. Run: python scripts/build_collector_batch.py')
        print('Batch collector matches all five public sources.')
    else:
        TARGET.write_text(expected, encoding='utf-8')
        print('Batch collector generated.')


if __name__ == '__main__':
    main()
