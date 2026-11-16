"""Retired selections must not expose routes or start collection/sync jobs."""
import pytest
from app.core.cache import MODULES
from test_collector_batch import batch


@pytest.mark.parametrize('path', ['/high-growth/latest', '/earnings-gap/stocks', '/earnings-gap/stocks/000001.XSHE'])
def test_retired_selection_routes_are_absent(client, path):
    assert client.get('/api/v1' + path).status_code == 404


def test_retired_selections_are_not_scheduled(client, batch):
    for module in ('high-growth', 'earnings-gap'):
        assert module not in MODULES
        assert module not in client.get('/api/v1/modules').text
    assert not hasattr(batch, '_growth_strategy')
    assert not hasattr(batch, '_earnings_strategy')
    assert set(batch.STRATEGY_ORDER) == {
        '行业动量', '抱团拥挤度', '市场情绪跟踪', '微盘温度计',
        '微盘新高新低', '高股息选股', 'ETF动量热度', '个股机构持仓', '估值标尺',
    }
    # Public-source collectors remain active in the app, not in the JoinQuant batch.
    for module in ('cash-cow', 'growth-dividend'):
        assert module in MODULES
