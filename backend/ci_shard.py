"""Deterministic pytest module shards; local pytest remains a complete run by default."""
from collections import Counter
import re

import pytest


def parse_shard(value):
    if not re.fullmatch(r'[1-9][0-9]*/[1-9][0-9]*', value):
        raise pytest.UsageError('--ci-shard must be INDEX/TOTAL, e.g. 1/3')
    index, total = map(int, value.split('/'))
    if not 1 <= index <= total <= 32:
        raise pytest.UsageError('--ci-shard requires 1 <= INDEX <= TOTAL <= 32')
    return index, total


def module_shards(nodeids, total):
    """Balance collected test counts, keeping module fixtures and test order intact."""
    counts = Counter(nodeid.split('::', 1)[0] for nodeid in nodeids)
    loads = [0] * total
    assignment = {}
    for module in sorted(counts, key=lambda name: (-counts[name], name)):
        shard = min(range(total), key=lambda i: (loads[i], i))
        assignment[module] = shard + 1
        loads[shard] += counts[module]
    return assignment


def pytest_addoption(parser):
    parser.getgroup('ci').addoption('--ci-shard', type=parse_shard, default=None,
                                   help='Run one complete-module shard: INDEX/TOTAL')


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    option = config.getoption('ci_shard')
    if option is None:
        return
    index, total = option
    assignment = module_shards([item.nodeid for item in items], total)
    selected, deselected = [], []
    for item in items:
        target = selected if assignment[item.nodeid.split('::', 1)[0]] == index else deselected
        target.append(item)
    if not selected:
        raise pytest.UsageError('CI shard has no tests; check the shard count and test selection')
    reporter = config.pluginmanager.getplugin('terminalreporter')
    if reporter:
        reporter.write_line(f'CI shard {index}/{total}: {len(selected)} of {len(items)} collected tests')
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
