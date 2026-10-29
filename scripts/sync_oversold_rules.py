"""Embed shared rules so exported research/collector files remain standalone."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START, END = '# BEGIN SHARED OVERSOLD RULES', '# END SHARED OVERSOLD RULES'
TARGETS = ['脚本/本地统计/超短线四策略胜率统计_本地版.py',
           'collectors/短线选股_数据库版.py', 'collectors/策略批量采集_数据库版.py']


def sync(check=False):
    source = (ROOT / 'strategy_rules/oversold.py').read_text()
    block = source[source.index(START):source.index(END) + len(END)]
    for name in TARGETS:
        path = ROOT / name
        text = path.read_text()
        if START in text:
            updated = text[:text.index(START)] + block + text[text.index(END) + len(END):]
        else:
            pos = text.index('\ndef ')
            updated = text[:pos] + '\n' + block + '\n\n' + text[pos:]
        if check:
            assert updated == text, name + ': run python scripts/sync_oversold_rules.py'
        else:
            path.write_text(updated)


if __name__ == '__main__':
    import sys
    sync('--check' in sys.argv)
