"""Embed the exact momentum threshold into standalone JoinQuant collectors."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START, END = '# BEGIN SHARED MOMENTUM THRESHOLD', '# END SHARED MOMENTUM THRESHOLD'
TARGETS = ['collectors/行业动量计算_数据库版.py', 'collectors/策略批量采集_数据库版.py']


def sync(check=False):
    source = (ROOT / 'collectors/rules/momentum_threshold.py').read_text(encoding='utf-8')
    block = source[source.index(START):source.index(END) + len(END)]
    for name in TARGETS:
        path = ROOT / name
        text = path.read_text(encoding='utf-8')
        if START in text:
            updated = text[:text.index(START)] + block + text[text.index(END) + len(END):]
        else:
            pos = text.index('# BEGIN SHARED') if '# BEGIN SHARED' in text else text.index('\ndef ')
            updated = text[:pos] + '\n' + block + '\n\n' + text[pos:]
        if check:
            assert updated == text, name + ': run python collectors/tools/sync_momentum_threshold.py'
        else:
            path.write_text(updated, encoding='utf-8')


if __name__ == '__main__':
    import sys
    sync('--check' in sys.argv)
