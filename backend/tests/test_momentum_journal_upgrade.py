"""A no-op collector upgrade must still enable bounded daily cache synchronization."""
import datetime as dt
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import event, inspect, select

from app.core.result_sync import HEAD, CHANGE, read_cursor
from app.models.momentum import IndustryMomentum
from test_cache import cached
from test_collector_batch import CONFIG, load

ROOT = Path(__file__).resolve().parents[2]
MODULE = 'industry-momentum'


@pytest.mark.parametrize('kind', ['standalone', 'batch'])
@pytest.mark.parametrize('end', ['2026-01-06', '2026-01-07', '2025-01-01'],
                         ids=['already-current', 'empty-calendar', 'before-window'])
def test_noop_upgrade_initializes_journal_without_recalculating(cached, engine, monkeypatch, kind, end):
    _, cache = cached
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    monkeypatch.setitem(sys.modules, 'mysql_connector', SimpleNamespace(
        get_database_config=lambda: dict(CONFIG), get_database_options=lambda: {},
        mysql_drivername=lambda _: 'mysql+pymysql', engine_options=lambda _: {}))
    name = '行业动量计算_数据库版.py' if kind == 'standalone' else '策略批量采集_数据库版.py'
    collector = load(ROOT / 'collectors' / name, 'noop_upgrade_' + kind)

    class Clock(dt.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 1, 7, 10)

    collector.datetime = SimpleNamespace(datetime=Clock, date=dt.date, timedelta=dt.timedelta)
    def calendar(**kwargs):
        if 'count' in kwargs:
            return [stamp.date() for stamp in pd.bdate_range(end=kwargs['end_date'], periods=kwargs['count'])]
        return []
    def forbidden(*args, **kwargs):
        pytest.fail('No new trading dates: must not request securities, industries or prices')
    apis = {key: forbidden for key in ('get_all_securities', 'get_industries', 'get_industry_stocks', 'get_price')}
    apis['get_trade_days'] = calendar
    if kind == 'standalone':
        for key, value in apis.items():
            setattr(collector, key, value)
        strategy = collector
    else:
        strategy = collector.STRATEGIES['行业动量'](apis, CONFIG)
    monkeypatch.setattr(strategy.MysqlMomentum, 'connect_to_mysql', lambda: engine)
    # The test owns this in-memory source beyond the collector's normal lifecycle.
    monkeypatch.setattr(engine, 'dispose', lambda: None)
    with engine.connect() as source:
        original = source.execute(select(IndustryMomentum.__table__)).all()
        assert not inspect(source).has_table(HEAD.name)
    assert strategy.run(end_date=end, history_mode=False).empty
    with engine.connect() as source:
        first = source.execute(select(HEAD).where(HEAD.c.module == MODULE)).mappings().one()
        assert first['revision'] == 0
        assert source.execute(select(CHANGE)).all() == []
        assert source.execute(select(IndustryMomentum.__table__)).all() == original
    assert strategy.run(end_date=end, history_mode=False).empty
    with engine.connect() as source:
        assert source.execute(select(HEAD).where(HEAD.c.module == MODULE)).mappings().one() == first

    # The first website sync establishes its receipt; the next performs no
    # business-table read or SQLite copy, even though the collector added no rows.
    cache.refresh(MODULE, background=False)
    before = cache.read_state()['modules'][MODULE]
    previous = cache.snapshot_path(MODULE, before)
    assert not before['error'] and not before['sync_note']
    assert read_cursor(previous, MODULE)['revision'] == 0
    statements = []
    def capture(conn, cursor, sql, *args):
        statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    monkeypatch.setattr('app.core.result_sync.shutil.copyfile', forbidden)
    try:
        cache.refresh(MODULE, background=False)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    after = cache.read_state()['modules'][MODULE]
    assert not after['error']
    assert cache.snapshot_path(MODULE, after) == previous
    assert not any('FROM industry_momentum' in sql or 'FROM result_sync_change' in sql for sql in statements)
