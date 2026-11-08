"""Keep quarterly transaction versions identical in both standalone collectors."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
START, END = '# BEGIN SHARED EARNINGS VERSION', '# END SHARED EARNINGS VERSION'


def sync_block(source_name, START, END, check):
    source = (ROOT/'collectors'/'rules'/source_name).read_text(encoding='utf-8')
    block = source[source.index(START):source.index(END)+len(END)]
    for name in ('净利润断层_数据库版.py', '策略批量采集_数据库版.py'):
        path = ROOT/'collectors'/name
        text = path.read_text(encoding='utf-8')
        if START in text:
            result = text[:text.index(START)] + block + text[text.index(END)+len(END):]
        else:
            pos = text.index('# BEGIN SHARED') if '# BEGIN SHARED' in text else text.index('\ndef ')
            result = text[:pos] + '\n' + block + '\n\n' + text[pos:]
        if check:
            assert result == text, name
        else:
            path.write_text(result, encoding='utf-8')


def sync(check=False):
    sync_block('earnings_version.py', START, END, check)
    sync_block('earnings_followup.py', '# BEGIN SHARED EARNINGS FOLLOWUP', '# END SHARED EARNINGS FOLLOWUP', check)


if __name__ == '__main__':
    import sys
    sync('--check' in sys.argv)
