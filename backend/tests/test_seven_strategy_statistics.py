"""Local DB integration, exact production windows, and deployment separation."""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text
from test_ultra_short_statistics import m, database, prices

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('impact', ROOT/'scripts/deployment_impact.py')
impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(impact)


def seed_three(engine, calendar, dates, code='000001.XSHE'):
    manifest, windows = [], []
    for day in dates:
        manifest.append(dict(signal_date=day.date(), universe_scope=m.THREE_SCOPE,
            universe_count=1, eligible_count=1, rule_version=m.THREE_RULE_VERSION,
            price_mode=m.THREE_PRICE_MODE, window_days=90))
        frame = prices([10.]*86+[10.,11.,11.2,11.3], calendar[calendar.get_loc(day)-89:calendar.get_loc(day)+1])
        frame.loc[frame.index[-2:], 'low'] = 11.1
        for date, row in frame.iterrows():
            windows.append(dict(signal_date=day.date(), code=code, trade_date=date.date(),
                                **{key: row[key] for key in ('open','high','low','close','volume')}))
    pd.DataFrame(manifest).to_sql('short_research_coverage', engine, index=False)
    pd.DataFrame(windows).to_sql('short_research_window', engine, index=False)


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    calendar = pd.bdate_range(end='2023-12-29', periods=160).append(pd.bdate_range('2024-01-02','2024-01-05'))
    database(engine, prices([10.]*160 + [10.,11.,10.,9.], calendar))
    seed_three(engine, calendar, calendar[160:])
    yield engine, calendar
    engine.dispose()


def test_seven_selection_default_and_unknown():
    assert m.START_DATE == '2024-01-01'
    assert len(m.resolve_strategies('all')) == 7
    assert m.resolve_strategies(['红悬停', '红悬停龙', '蓄力龙']) == ['红悬停龙', '蓄力龙']
    assert m.resolve_strategies(None) == m.STRATEGIES
    with pytest.raises(ValueError, match='未知策略'):
        m.resolve_strategies('typo')


def test_seven_database_end_to_end_read_only_and_mature_denominators(db, tmp_path):
    engine, calendar = db
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda conn,cursor,sql,*args: statements.append(sql))
    result = m.run(engine=engine, strategies='all', output_dir=tmp_path, show_progress=False)
    assert len(result['summary']) == 21
    group = result['summary'][result['summary']['策略'] == '红悬停龙']
    assert group['有效样本'].tolist() == [3,2,1]
    assert group['待观察'].tolist() == [1,2,3]
    details = result['details'][result['details']['strategy'] == '红悬停龙']
    assert details['signal_date'].tolist() == list(calendar[160:])
    assert details.iloc[0]['d1_return_pct'] == pytest.approx(10)
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert len(list(result['output_dir'].glob('*.csv'))) == 5
    metadata = json.loads((result['output_dir']/'运行参数.json').read_text())
    assert len(metadata['strategies']) == 7 and 'dbpassword' not in str(metadata)


def test_signal_independent_of_future_prices_and_date_filter(db, tmp_path):
    engine, calendar = db
    first = m.run(engine=engine, strategies='红悬停', start_date='2024-01-01', end_date='2024-01-02',
                  as_of='2024-01-03', output_dir=tmp_path, show_progress=False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ultra_research_bar SET adj_close=99999 WHERE trade_date>'2024-01-02'"))
    second = m.run(engine=engine, strategies='红悬停', start_date='2024-01-01', end_date='2024-01-02',
                   as_of='2024-01-05', output_dir=tmp_path, show_progress=False)
    assert first['details']['signal_date'].tolist() == second['details']['signal_date'].tolist() == [calendar[160]]
    assert first['details'].iloc[0]['d2_status'] == '待观察'
    assert second['details'].iloc[0]['d2_status'] == '有效'


@pytest.mark.parametrize('sql,match', [
    ('DROP TABLE short_research_window', '前复权90日窗口'),
    ("UPDATE short_research_coverage SET universe_scope='shsz_only'", '范围'),
    ("UPDATE short_research_coverage SET universe_count=2", '数量'),
    ("DELETE FROM short_research_coverage WHERE signal_date='2024-01-02'", '缺少信号交易日'),
    ("DELETE FROM short_research_window WHERE trade_date='2024-01-02'", '缺日'),
    ("UPDATE short_research_window SET close=NULL", '数值无效'),
    ("UPDATE ultra_research_universe SET is_st=NULL WHERE trade_date='2024-01-02'", 'ST'),
    ('DROP TABLE ultra_research_bar', '缺少表/字段'),
    ('ALTER TABLE ultra_research_bar DROP COLUMN adj_close', '缺少表/字段'),
])
def test_missing_sources_fail_without_partial_reports(db, tmp_path, sql, match):
    engine, _ = db
    with engine.begin() as conn:
        conn.execute(text(sql))
    with pytest.raises(ValueError, match=match):
        m.run(engine=engine, strategies='all', output_dir=tmp_path, show_progress=False)
    assert not list(tmp_path.iterdir())


def test_only_four_does_not_require_three_tables(db, tmp_path):
    engine, _ = db
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE short_research_window'))
        conn.execute(text('DROP TABLE short_research_coverage'))
    result = m.run(engine=engine, strategies=m.STRATEGIES[1], output_dir=tmp_path, show_progress=False)
    assert len(result['summary']) == 3


def test_three_production_function_ast_parity():
    local = ast.parse((ROOT/'脚本/本地统计/七策略胜率统计_本地版.py').read_text())
    prod = ast.parse((ROOT/'collectors/短线选股_数据库版.py').read_text())
    factory = next(n for n in local.body if isinstance(n,ast.FunctionDef) and n.name=='make_three_selector')
    original = next(n for n in prod.body if isinstance(n,ast.FunctionDef) and n.name=='_short_term_strategy')
    originals = {n.name: ast.dump(n) for n in original.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
    class Normalize(ast.NodeTransformer):
        def visit_Attribute(self,node):
            if isinstance(node.value,ast.Name) and node.value.id=='builtins':
                return ast.copy_location(ast.Name(id=node.attr,ctx=node.ctx),node)
            return self.generic_visit(node)
    for node in factory.body:
        if isinstance(node,(ast.FunctionDef,ast.ClassDef)):
            assert ast.dump(Normalize().visit(node)) == originals[node.name]


def test_rounding_prevents_naive_post_substitution():
    # Original round(pre_close*1.1, 2) is not invariant under scaling.
    pre, scale = 10.005, 3
    assert round(pre*1.1,2)*scale != round(pre*scale*1.1,2)


def test_cli_list_and_legacy_entry_need_no_config():
    for filename in ('七策略胜率统计_本地版.py','超短线四策略胜率统计_本地版.py'):
        result = subprocess.run([sys.executable, str(ROOT/'脚本/本地统计'/filename), '--list-strategies'],
                                capture_output=True,text=True,check=True)
        assert len(result.stdout.splitlines()) == 7


@pytest.mark.parametrize('paths,expected', [
    (['脚本/本地统计/七策略胜率统计_本地版.py'],False),
    (['docs/seven-strategy-statistics.md','backend/tests/test_seven_strategy_statistics.py'],False),
    (['脚本/本地统计/七策略胜率统计_本地版.py','backend/app/main.py'],True),
    (['scripts/deploy.py'],True), (['scripts/sync_oversold_rules.py'],True),
    (['strategy_rules/oversold.py'],True), (['frontend/package-lock.json'],True),
    (['backend/requirements.lock.txt'],True), (['.github/workflows/deploy.yml'],True),
    (['脚本/本地统计/unknown.py'],True), ([],True),
    (['脚本/本地统计/七策略胜率统计_本地版.py','renamed_runtime.py'],True),
])
def test_impact_paths(paths, expected):
    assert impact.requires_deployment(paths) is expected


def test_impact_invalid_missing_or_nonancestor_defaults_to_deploy(tmp_path):
    assert impact.compare('', 'a'*40, tmp_path)[0]
    assert impact.compare('0'*40, 'a'*40, tmp_path)[0]
    assert impact.compare('a'*40, 'b'*40, tmp_path)[0]


def test_real_git_research_diff_then_mixed_diff(tmp_path):
    def git(*args):
        return subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True,text=True).stdout.strip()
    git('init','-q');git('config','user.email','test@example.invalid');git('config','user.name','Test')
    (tmp_path/'README').write_text('base')
    git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
    target=tmp_path/'docs/seven-strategy-statistics.md';target.parent.mkdir();target.write_text('research')
    git('add','.');git('commit','-qm','research');head=git('rev-parse','HEAD')
    assert impact.compare(base,head,tmp_path)[0] is False
    (tmp_path/'README').write_text('runtime change')
    git('add','.');git('commit','-qm','mixed');head=git('rev-parse','HEAD')
    assert impact.compare(base,head,tmp_path)[0] is True


def test_workflow_keeps_ci_and_manual_deploy():
    workflow=(ROOT/'.github/workflows/deploy.yml').read_text()
    assert 'paths-ignore' not in workflow and 'needs: [backend, frontend]' in workflow
    assert "needs.backend.outputs.deploy_required != 'false'" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.operation == 'deploy' && vars.DEPLOY_ENABLED == 'true'" in workflow
    assert 'fetch-depth: 0' in workflow


def test_bse_pool_not_silently_removed(db, tmp_path):
    engine, _ = db
    with engine.begin() as conn:
        for table in ('ultra_research_universe', 'ultra_research_bar', 'short_research_window'):
            conn.execute(text("UPDATE {} SET code='830001.XBJ'".format(table)))
    result = m.run(engine=engine, strategies='红悬停', output_dir=tmp_path, show_progress=False)
    assert set(result['details']['code']) == {'830001.XBJ'}


def test_future_row_in_signal_window_rejected(db, tmp_path):
    engine, _ = db
    with engine.begin() as conn:
        conn.execute(text("UPDATE short_research_window SET trade_date='2024-01-03' WHERE signal_date='2024-01-02' AND trade_date='2024-01-02'"))
    with pytest.raises(ValueError, match='越界'):
        m.run(engine=engine, strategies='three', output_dir=tmp_path, show_progress=False)


def test_observation_missing_and_paused_excluded(db, tmp_path):
    engine, _ = db
    with engine.begin() as conn:
        conn.execute(text("UPDATE ultra_research_bar SET paused=1 WHERE trade_date='2024-01-03'"))
        conn.execute(text("DELETE FROM ultra_research_bar WHERE trade_date='2024-01-04'"))
    result = m.run(engine=engine, strategies='红悬停', end_date='2024-01-02',
                   output_dir=tmp_path, show_progress=False)
    assert result['summary']['有效样本'].tolist() == [0,0,1]
    assert result['details'].iloc[0]['d1_status'] == '停牌'
    assert result['details'].iloc[0]['d2_status'] == '缺少行情'


def test_only_three_needs_exactly_90_day_window(db, tmp_path):
    engine, calendar = db
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM ultra_research_calendar WHERE trade_date<:first'),
                     {'first':calendar[71].date()})
    result=m.run(engine=engine, strategies='红悬停', output_dir=tmp_path, show_progress=False)
    assert len(result['details']) == 4
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM ultra_research_calendar WHERE trade_date=:first'),
                     {'first':calendar[71].date()})
    with pytest.raises(ValueError, match='预热'):
        m.run(engine=engine, strategies='红悬停', output_dir=tmp_path, show_progress=False)


def test_expanded_db_cannot_expand_four_strategy_pool(db, tmp_path):
    engine, _ = db
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ultra_research_universe SELECT trade_date, '830001.XBJ', display_name, listed_date, eligible, is_st, market_cap FROM ultra_research_universe"))
        conn.execute(text("UPDATE short_research_coverage SET universe_count=2, eligible_count=2"))
    result=m.run(engine=engine, strategies='four', output_dir=tmp_path, show_progress=False)
    coverage=pd.read_csv(result['output_dir']/'数据覆盖.csv')
    assert coverage['code'].tolist() == ['000001.XSHE']


def test_precise_config_is_reused_without_new_keys(tmp_path, monkeypatch):
    config=tmp_path/'private.json'
    config.write_text((ROOT/'脚本/本地统计/数据库配置.example.json').read_text())
    monkeypatch.setattr(m, 'DEFAULT_CONFIG_PATH', config)
    settings=m.load_local_config()
    assert settings['DB_CONFIG']['host']=='your_mysql_host'
    assert set(settings)=={'DB_CONFIG','DB_TLS_VERIFY','DB_TLS_CA_PEM'}


def test_predefined_manifest_does_not_allow_duplicate_or_wrong_day(db, tmp_path):
    engine, _ = db
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO short_research_coverage SELECT * FROM short_research_coverage WHERE signal_date='2024-01-02'"))
    with pytest.raises(ValueError, match='重复'):
        m.run(engine=engine, strategies='three', output_dir=tmp_path, show_progress=False)
