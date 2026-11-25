"""Run reproducible, incremental Python quality gates without rewriting source.

The recorded debt is a set of source-line fingerprints, not a broad rule/file
exception. Moving an existing diagnostic does not create a new one; changing
its source or introducing a new diagnostic does. Baselines may only shrink.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
BASELINE = BACKEND / 'quality-baseline.json'


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=BACKEND, capture_output=True, text=True, check=False)


def python_files() -> list[str]:
    # Explicit project roots; never traverse logs, user configs, virtualenvs,
    # node_modules or generated caches.
    paths = list(BACKEND.glob('*.py'))
    for directory in ('backend/app', 'backend/tests', 'collectors', 'scripts'):
        paths.extend((ROOT / directory).rglob('*.py'))
    return sorted(p.relative_to(ROOT).as_posix() for p in paths if p.is_file())


def fingerprint(path: str, item: dict[str, Any]) -> str:
    lines = (ROOT / path).read_text(encoding='utf-8-sig').splitlines()
    row = item['location']['row']
    source = lines[row - 1].strip() if 0 < row <= len(lines) else ''
    payload = json.dumps([item['code'], item['message'], source], ensure_ascii=False)
    return f'{item["code"]}:{hashlib.sha256(payload.encode()).hexdigest()[:20]}'


def collect() -> dict[str, Any]:
    paths = python_files()
    args = [str(ROOT / path) for path in paths]
    lint = run(
        sys.executable,
        '-m',
        'ruff',
        'check',
        '--no-cache',
        '--output-format=json',
        '--config',
        str(BACKEND / 'pyproject.toml'),
        *args,
    )
    if lint.returncode not in (0, 1):
        raise RuntimeError(f'Ruff failed to run: {lint.stderr}')
    debt: dict[str, Counter[str]] = {}
    for item in json.loads(lint.stdout):
        path = Path(item['filename']).relative_to(ROOT).as_posix()
        code = item['code']
        if not code:
            raise RuntimeError(f'Ruff found invalid syntax in {path}')
        key = fingerprint(path, item)
        debt.setdefault(path, Counter())[key] += 1

    fmt = run(
        sys.executable,
        '-m',
        'ruff',
        'format',
        '--check',
        '--no-cache',
        '--config',
        str(BACKEND / 'pyproject.toml'),
        *args,
    )
    if fmt.returncode not in (0, 1):
        raise RuntimeError(f'Ruff formatter failed to run: {fmt.stderr}')
    unformatted = []
    for line in fmt.stdout.splitlines():
        if line.startswith('Would reformat: '):
            p = Path(line.removeprefix('Would reformat: '))
            unformatted.append(
                (p if p.is_absolute() else BACKEND / p).resolve().relative_to(ROOT).as_posix()
            )
    if fmt.returncode == 1 and not unformatted:
        raise RuntimeError('Ruff format failed without a recognized diagnostic')
    return {
        'version': 1,
        'lint': {p: dict(sorted(c.items())) for p, c in sorted(debt.items())},
        'format': sorted(unformatted),
    }


def additions(current: dict[str, Any], base: dict[str, Any]) -> list[str]:
    if current.get('version') != 1 or base.get('version') != 1:
        raise ValueError('Unknown quality baseline version')
    result = []
    for path, entries in sorted(current['lint'].items()):
        excess = Counter(entries) - Counter(base['lint'].get(path, {}))
        for fingerprint_key, count in sorted(excess.items()):
            result.append(f'{path}: new {fingerprint_key.split(":")[0]} diagnostic ({count})')
    for path in sorted(set(current['format']) - set(base['format'])):
        result.append(f'{path}: new formatting debt; run ruff format')
    return result


def previous_baseline(base: str) -> dict[str, Any] | None:
    if not re.fullmatch('[0-9a-f]{40}', base):
        raise ValueError('--base must be a full Git commit SHA')
    exists = run('git', 'cat-file', '-e', f'{base}^{{commit}}')
    if exists.returncode:
        raise RuntimeError('Quality comparison commit is unavailable')
    result = run('git', 'show', f'{base}:backend/quality-baseline.json')
    if result.returncode:
        # A branch introducing the gate has no pre-existing debt record.
        probe = run('git', 'ls-tree', '--full-tree', base, '--', 'backend/quality-baseline.json')
        if probe.returncode or probe.stdout.strip():
            raise RuntimeError('Could not read comparison quality baseline')
        return None
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', help='Full Git SHA used to prevent adding legacy debt')
    parser.add_argument(
        '--prune', action='store_true', help='Remove resolved baseline entries only'
    )
    args = parser.parse_args()
    current = collect()
    baseline = json.loads(BASELINE.read_text())
    errors = additions(current, baseline)
    if args.base:
        old = previous_baseline(args.base)
        if old is not None:
            errors += [f'Baseline increase: {item}' for item in additions(baseline, old)]
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(
            'Quality gate failed. Fix new debt; baseline additions are not an acceptable fix.',
            file=sys.stderr,
        )
        return 1
    if args.prune:
        BASELINE.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
    print(
        f'Ruff: {sum(sum(c.values()) for c in current["lint"].values())} existing diagnostics; '
        f'{len(current["format"])} legacy unformatted files; no new debt.'
    )
    typed = run(sys.executable, '-m', 'mypy', '--no-incremental')
    print(typed.stdout, end='')
    print(typed.stderr, end='', file=sys.stderr)
    return typed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
