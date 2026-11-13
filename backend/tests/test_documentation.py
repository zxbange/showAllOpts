"""Repository documentation must remain navigable after directory changes."""
from pathlib import Path
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]


def test_documentation_local_links_resolve():
    documents = [ROOT / 'README.md', ROOT / 'collectors/README.md', ROOT / 'scripts/README.md',
                 *sorted((ROOT / 'docs').rglob('*.md'))]
    broken = []
    for document in documents:
        content = re.sub(r'```.*?```', '', document.read_text(encoding='utf-8'), flags=re.S)
        for href in re.findall(r'\]\(([^\s)]+)\)', content):
            if href.startswith(('#', 'https:', 'http:', 'mailto:')):
                continue
            target = unquote(href.split('#', 1)[0])
            if target and not (document.parent / target).exists():
                broken.append(f'{document.relative_to(ROOT)} -> {target}')
    assert not broken, '\n'.join(broken)
