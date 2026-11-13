"""Removal must not create jobs, serve old endpoints, or erase old storage."""
from pathlib import Path
import json
import pytest
from sqlalchemy import inspect
from app.core.cache import MODULES, SnapshotCache
from app.core.config import Settings
from test_collector_batch import batch


@pytest.mark.parametrize('path', ['/short-term/latest', '/short-term/validation/jobs',
    '/short-term/validation/sources', '/short-term/validation/jobs/old/retry',
    '/short-term/validation/sources/test'])
def test_retired_api_not_registered(client, path):
    for method in ('get', 'post'):
        assert getattr(client, method)('/api/v1' + path).status_code == 404
    assert not hasattr(client.app.state, 'short_validation')


def test_catalogue_cache_and_models_exclude_module(client, engine, tmp_path):
    assert 'short-term' not in MODULES
    assert not any('short-term' in getattr(route, 'path', '') for route in client.app.routes)
    assert not any(name.startswith(('short_term_', 'ultra_research_')) for name in inspect(engine).get_table_names())
    assert 'short-term' not in client.get('/api/v1/modules').text
    cache = SnapshotCache(Settings(_env_file=None, log_dir=tmp_path/'log'), engine)
    cache.root.mkdir(parents=True)
    old = cache.root/'short-term-retired.sqlite'
    old.write_bytes(b'historical-data-not-to-delete')
    cache.write_state({'modules': {'short-term': {'file': old.name, 'updated_at': '2026-01-01'}}})
    assert all(row['id'] != 'short-term' for row in cache.status()['modules'])
    cache.cleanup(cache.read_state())
    assert old.read_bytes() == b'historical-data-not-to-delete'


def test_batch_keeps_current_collectors_without_restoring_short_term(batch):
    expected = {'行业动量','抱团拥挤度','市场情绪跟踪','微盘温度计','净利润断层',
                '微盘新高新低','高股息选股','高增长','ETF动量热度','个股机构持仓','估值标尺','创红比'}
    assert set(batch.STRATEGY_SPECS) == expected
    # ETF份额 runs through the exchange collector and has no legacy batch entry.
    assert set(batch.STRATEGY_ORDER) == expected
    assert not hasattr(batch, '_short_term_strategy')
