"""Deployment gate: known runtime changes deploy; uncertain comparisons block publication."""
import argparse
import os
from pathlib import Path
import re
import subprocess

# User-approved directory scope plus previously approved independent docs/tests.
NO_DEPLOY_PREFIXES = ('脚本/', 'scripts/')
NO_DEPLOY_PATHS = frozenset({
    '脚本/本地统计/七策略胜率统计_本地版.py',
    '脚本/本地统计/超短线四策略胜率统计_本地版.py',
    '脚本/本地统计/短线三策略胜率验证_研究版.py',
    '脚本/本地统计/数据库配置.example.json',
    'docs/seven-strategy-statistics.md',
    'docs/ultra-short-statistics.md',
    'docs/short-term-three-validation.md',
    'docs/short-term-winrate.md',
    '脚本/脚本使用说明.md',
    'backend/tests/test_seven_strategy_statistics.py',
    'backend/tests/test_ultra_short_statistics.py',
    'backend/tests/test_three_strategy_validation.py',
})


def requires_deployment(paths):
    paths = list(paths)
    return not paths or any(not path.startswith(NO_DEPLOY_PREFIXES) and path not in NO_DEPLOY_PATHS
                            for path in paths)


def compare(base, head, cwd=None):
    if any(not re.fullmatch(r'[0-9a-f]{40}', sha or '') or sha == '0'*40 for sha in (base, head)):
        return None, 'missing or invalid comparison SHA; automatic publication blocked'
    try:
        subprocess.run(['git', 'merge-base', '--is-ancestor', base, head], cwd=cwd,
                       check=True, capture_output=True, timeout=30)
        result = subprocess.run(['git', 'diff', '--no-renames', '--name-only', '-z', base, head, '--'],
                                cwd=cwd, check=True, capture_output=True, timeout=30)
        paths = result.stdout.decode('utf-8').rstrip('\0').split('\0') if result.stdout else []
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None, 'comparison unavailable; automatic publication blocked'
    if not paths:
        return None, 'empty comparison; automatic publication blocked'
    required = requires_deployment(paths)
    return required, 'change outside approved directories/docs/tests' if required else 'only approved script directories/docs/tests'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=os.environ.get('BEFORE_SHA', ''))
    parser.add_argument('--head', default=os.environ.get('HEAD_SHA', ''))
    args = parser.parse_args()
    required, reason = compare(args.base, args.head)
    if required is None:
        print('Deployment impact unknown: ' + reason)
        return 2
    value = 'true' if required else 'false'
    print('deploy_required={}: {}'.format(value, reason))
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
            stream.write('deploy_required={}\n'.format(value))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
