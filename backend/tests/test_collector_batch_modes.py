"""Batch-only execution modes, using SQLite and the standalone source fixtures."""
import ast
from types import SimpleNamespace

import pytest

from test_collector_batch import CONFIG, TARGET, batch
from test_microcap_collector import collector as micro_source
from test_etf_collector import collector as etf_source
import test_microcap_collector as micro
import test_etf_collector as etf


def strategy_env(batch, name, source, monkeypatch):
    # Execute the unchanged factory body at module scope so source adapters can be
    # injected without changing production signatures or touching closure cells.
    tree = ast.parse(TARGET.read_text(encoding='utf-8'))
    factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == batch.STRATEGIES[name].__name__)
    assert isinstance(factory.body[-1], ast.Return)
    tree.body = factory.body[:-1]
    env = dict(vars(batch), _apis=source.env, _config=CONFIG)
    exec(compile(tree, str(TARGET), 'exec'), env)
    env.update(pd=source.env['pd'], china_now=source.env['china_now'])
    monkeypatch.setattr(batch, 'connect_database', lambda _: source.engine)
    return SimpleNamespace(**dict(vars(source), env=env))


@pytest.mark.parametrize('name', ['行业动量', '抱团拥挤度', '微盘温度计', 'ETF份额', '净利润断层'])
def test_overwrite_rejected_before_source_or_database_access(batch, monkeypatch, name):
    monkeypatch.setattr(batch, 'connect_database', lambda _: pytest.fail('must not connect'))
    strategy = batch.STRATEGIES[name]({}, CONFIG)
    with pytest.raises(ValueError, match='skip_existing'):
        strategy.run(skip_existing=False)


@pytest.mark.parametrize('name,key,value', [('微盘温度计', 'sample_step', 5), ('ETF份额', 'lookback_days', 5)])
def test_removed_options_cannot_silently_change_collection(batch, monkeypatch, name, key, value):
    monkeypatch.setattr(batch, 'connect_database', lambda _: pytest.fail('must not connect'))
    strategy = batch.STRATEGIES[name]({}, CONFIG)
    with pytest.raises(TypeError, match=key):
        strategy.run(**{key: value})
    batch.STRATEGY_CONFIG[name][key] = value
    with pytest.raises(ValueError, match=key):
        batch.STRATEGIES[name]({}, CONFIG)


@pytest.fixture
def daily_batch(batch, micro_source, monkeypatch):
    c = strategy_env(batch, '微盘温度计', micro_source, monkeypatch)
    c.env['calculate_period'] = micro_source.env['calculate_period']
    micro.daily_index(c)
    return c


def test_batch_daily_migration_reuses_legacy_values(daily_batch, micro_source):
    c = daily_batch
    micro_source.env['run'](sample_step=5, micro_num=2)
    legacy = micro.saved(micro_source)
    c.calculated.clear()
    result = c.env['run'](micro_num=2)
    assert len(result) == len(c.days)
    assert c.calculated == [day for day in c.days if day not in c.days[::5]]
    assert [r for r in micro.saved(c) if r['sample_step'] == 5] == legacy
    daily = micro.daily_rows(c)
    assert [r['trade_date'] for r in daily] == c.days
    assert [r['sample_count'] for r in daily] == list(range(1, len(c.days) + 1))
    # A clean daily calculation must produce exactly the same values as migration.
    with c.Session() as session:
        session.query(c.env['MicrocapTemperature']).delete()
        session.commit()
    c.env['run'](micro_num=2)
    assert micro.daily_rows(c) == daily


def test_batch_daily_noop_reads_no_members_or_prices_and_writes_nothing(daily_batch):
    micro.test_daily_noop_does_not_read_prices_constituents_or_write(daily_batch)


@pytest.mark.parametrize('shift_counts', [True, False])
def test_batch_daily_gap_and_interrupted_repair_resume(daily_batch, shift_counts):
    micro.test_daily_historical_gap_repairs_only_dependent_fields_and_can_resume_repair(daily_batch, shift_counts)


def test_batch_daily_insert_failure_rolls_back_pending_marker(daily_batch):
    micro.test_daily_hole_insert_and_pending_marker_rollback_together(daily_batch)


def test_batch_daily_only_uses_closed_days(daily_batch):
    micro.test_daily_before_close_defers_only_today(daily_batch)


def test_batch_daily_initial_failure_and_incremental_resume(daily_batch):
    micro.test_daily_initial_failure_and_next_day_incremental(daily_batch)


def test_batch_etf_incremental_and_manual_backfill_preserve_existing(batch, etf_source, monkeypatch):
    c = strategy_env(batch, 'ETF份额', etf_source, monkeypatch)
    etf.test_insert_only_etf_requests_only_new_dates_and_protects_existing(c)
    before = [(r.trade_date, r.shares, r.updated_at) for r in etf.rows(c)]
    c.requested.clear()
    # Manual historical scans remain available for filling holes, without overwrites.
    c.source['510300.XSHG', c.days[1]] = 999
    assert c.env['run'](start_date=c.days[1], incremental=False) == []
    assert c.requested[0][1] == c.days[1]
    assert [(r.trade_date, r.shares, r.updated_at) for r in etf.rows(c)] == before
