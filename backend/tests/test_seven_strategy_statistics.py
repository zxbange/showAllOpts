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
    (['scripts/deploy.py'],False), (['scripts/sync_oversold_rules.py'],False),
    (['strategy_rules/oversold.py'],True), (['frontend/package-lock.json'],True),
    (['backend/requirements.lock.txt'],True), (['.github/workflows/deploy.yml'],True),
    (['脚本/本地统计/unknown.py'],False), ([],True),
    (['脚本/本地统计/七策略胜率统计_本地版.py','renamed_runtime.py'],True),
])
def test_impact_paths(paths, expected):
    assert impact.requires_deployment(paths) is expected


def test_impact_invalid_missing_or_nonancestor_blocks_deploy(tmp_path):
    assert impact.compare('', 'a'*40, tmp_path)[0] is None
    assert impact.compare('0'*40, 'a'*40, tmp_path)[0] is None
    assert impact.compare('a'*40, 'b'*40, tmp_path)[0] is None


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
    assert "needs.backend.outputs.deploy_required == 'true'" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.operation == 'deploy' && vars.DEPLOY_ENABLED == 'true'" in workflow
    assert 'fetch-depth: 0' in workflow




@pytest.fixture
def db():
    engine=create_engine('sqlite:///:memory:')
    calendar=pd.bdate_range(end='2023-12-29',periods=160).append(pd.bdate_range('2024-01-02','2024-01-08'))
    frame=prices([10.]*158+[11.,11.2,11.3,11.4,11.3,11.4,11.5],calendar)
    frame.loc[calendar[159:],'low']=11.1
    database(engine,frame)
    yield engine,calendar
    engine.dispose()


def test_existing_three_tables_only_all_seven_and_notice(db,tmp_path,capsys):
    engine,calendar=db
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,*args: statements.append(sql))
    result=m.run(engine=engine,strategies='all',output_dir=tmp_path,show_progress=False)
    assert len(result['summary'])==21
    details=result['details'][result['details'].strategy=='红悬停龙']
    assert details.signal_date.tolist()==[calendar[160]]
    assert details.iloc[0].d1_return_pct==pytest.approx((11.4/11.3-1)*100)
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert all('short_research' not in sql and 'short_term_stock' not in sql for sql in statements)
    assert '统一四策略数据口径的验证' in capsys.readouterr().out
    meta=json.loads((result['output_dir']/'运行参数.json').read_text())
    assert '不是原三策略生产信号的精确复现' in meta['validation_notice']
    assert 'dbpassword' not in str(meta)


@pytest.mark.parametrize('choice,count', [('红悬停',3),(['红悬停','蓄力龙'],6),('three',9),('four',12),('all',21)])
def test_choose_single_multiple_all(db,tmp_path,choice,count):
    result=m.run(engine=db[0],strategies=choice,output_dir=tmp_path,show_progress=False)
    assert len(result['summary'])==count
    assert set(result['summary']['策略'])==set(m.resolve_strategies(choice))


def test_no_future_data_and_maturity(db,tmp_path):
    engine,calendar=db
    first=m.run(engine=engine,strategies='three',end_date='2024-01-02',as_of='2024-01-03',output_dir=tmp_path,show_progress=False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ultra_research_bar SET adj_close=999999, adj_open=999999 WHERE trade_date>'2024-01-02'"))
    second=m.run(engine=engine,strategies='three',end_date='2024-01-02',as_of='2024-01-05',output_dir=tmp_path,show_progress=False)
    assert first['details'].signal_date.tolist()==second['details'].signal_date.tolist()==[calendar[160]]
    assert first['details'].iloc[0].d2_status=='待观察'
    assert second['details'].iloc[0].d2_status=='有效'


@pytest.mark.parametrize('column,value', [('is_st',1),('market_cap',29),('listed_date','2023-12-01'),('eligible',0)])
def test_three_uses_four_universe_filters(db,tmp_path,column,value):
    engine,_=db
    with engine.begin() as conn:
        conn.execute(text('UPDATE ultra_research_universe SET {}=:value'.format(column)),{'value':value})
    with pytest.raises(ValueError,match='合格股票快照|基础样本'):
        m.run(engine=engine,strategies='three',output_dir=tmp_path,show_progress=False)


@pytest.mark.parametrize('column,value',[('paused',1),('money',49999999),('volume',0)])
def test_three_uses_four_signal_eligibility(db,tmp_path,column,value):
    engine,_=db
    with engine.begin() as conn:
        conn.execute(text("UPDATE ultra_research_bar SET {}=:value WHERE trade_date='2024-01-02'".format(column)),{'value':value})
    result=m.run(engine=engine,strategies='three',output_dir=tmp_path,show_progress=False)
    assert result['details'].empty


def test_uses_post_prices_not_unadjusted_prices(db,tmp_path):
    engine,_=db
    with engine.begin() as conn:
        conn.execute(text('UPDATE ultra_research_bar SET open=1,high=1,low=1,close=1'))
    result=m.run(engine=engine,strategies='红悬停',output_dir=tmp_path,show_progress=False)
    assert len(result['details'])==1
    assert result['details'].iloc[0].signal_close==11.3
    assert result['details'].iloc[0].signal_raw_close==1


@pytest.mark.parametrize('sql,match',[
    ('DROP TABLE ultra_research_bar','缺少表/字段'),
    ('ALTER TABLE ultra_research_bar DROP COLUMN adj_close','缺少表/字段'),
    ("DELETE FROM ultra_research_calendar WHERE trade_date<'2023-12-01'",'预热'),
])
def test_missing_schema_and_prewarm_fail(db,tmp_path,sql,match):
    with db[0].begin() as conn:conn.execute(text(sql))
    with pytest.raises(ValueError,match=match):m.run(engine=db[0],strategies='all',output_dir=tmp_path,show_progress=False)
    assert not list(tmp_path.iterdir())


def test_missing_90_day_context_is_not_silently_shortened(db,tmp_path):
    engine,calendar=db
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM ultra_research_bar WHERE trade_date=:day'),{'day':calendar[80].date()})
    with pytest.raises(ValueError,match='预热行情缺失'):
        m.run(engine=engine,strategies='three',output_dir=tmp_path,show_progress=False)


def test_four_scope_excludes_outside_market(db,tmp_path):
    engine,_=db
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ultra_research_universe SELECT trade_date, '830001.XBJ', display_name, listed_date, eligible, is_st, market_cap FROM ultra_research_universe"))
    result=m.run(engine=engine,strategies='all',output_dir=tmp_path,show_progress=False)
    assert pd.read_csv(result['output_dir']/'数据覆盖.csv')['code'].tolist()==['000001.XSHE']


def test_observation_missing_paused_and_strict_denominators(db,tmp_path):
    engine,_=db
    with engine.begin() as conn:
        conn.execute(text("UPDATE ultra_research_bar SET paused=1 WHERE trade_date='2024-01-03'"))
        conn.execute(text("DELETE FROM ultra_research_bar WHERE trade_date='2024-01-04'"))
    result=m.run(engine=engine,strategies='红悬停',end_date='2024-01-02',output_dir=tmp_path,show_progress=False)
    assert result['summary']['有效样本'].tolist()==[0,0,1]
    assert result['details'].iloc[0].d1_status=='停牌'
    assert result['details'].iloc[0].d2_status=='缺少行情'


def test_impact_comparison_failure_blocks_without_output(tmp_path):
    import os
    output=tmp_path/'output'
    proc=subprocess.run([sys.executable,str(ROOT/'scripts/deployment_impact.py'),'--base','invalid','--head','a'*40],
        capture_output=True,text=True,env=dict(os.environ,GITHUB_OUTPUT=str(output)))
    assert proc.returncode==2 and 'blocked' in proc.stdout
    assert not output.exists()


def test_same_commit_is_unknown_not_auto_deploy():
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()
    assert impact.compare(sha,sha,ROOT)[0] is None


def test_configuration_keys_still_compatible(tmp_path,monkeypatch):
    path=tmp_path/'config.json';path.write_text((ROOT/'脚本/本地统计/数据库配置.example.json').read_text())
    monkeypatch.setattr(m,'DEFAULT_CONFIG_PATH',path)
    assert set(m.load_local_config())=={'DB_CONFIG','DB_TLS_VERIFY','DB_TLS_CA_PEM'}


@pytest.mark.parametrize('field,value,table', [
    ('is_st',1,'ultra_research_universe'),('market_cap',29,'ultra_research_universe'),
    ('listed_date','2023-12-01','ultra_research_universe'),('eligible',0,'ultra_research_universe'),
    ('paused',1,'ultra_research_bar'),('money',49999999,'ultra_research_bar'),('volume',0,'ultra_research_bar'),
])
def test_all_seven_share_real_pool_and_denominator(db,tmp_path,monkeypatch,field,value,table):
    from types import SimpleNamespace
    engine,calendar=db
    original=m.pattern_candidates
    def every_pattern(frame,params):
        candidates=original(frame,params)
        for name in m.STRATEGIES:
            candidates[name]=True
        return candidates
    monkeypatch.setattr(m,'pattern_candidates',every_pattern)
    monkeypatch.setattr(m,'make_three_selector',lambda:SimpleNamespace(evaluate=lambda frame,code:list(m.THREE_METHODS)))
    with engine.begin() as conn:
        conn.execute(text("UPDATE {} SET {}=:value WHERE trade_date='2024-01-02'".format(table,field)),{'value':value})
    result=m.run(engine=engine,strategies='all',params={'strength_quantile':0},output_dir=tmp_path,show_progress=False)
    groups=[g.signal_date.tolist() for _,g in result['details'].groupby('strategy')]
    assert len(groups)==7 and all(dates==list(calendar[161:]) for dates in groups)
    # All signals deliberately identical: actual production summary code must yield equal denominators.
    denominators=[g['有效样本'].tolist() for _,g in result['summary'].groupby('策略')]
    assert all(counts==[3,2,1] for counts in denominators)
    meta=json.loads((result['output_dir']/'运行参数.json').read_text())
    assert meta['shared_base_samples']==4


def test_gap_inside_common_readiness_is_explicit(db,tmp_path):
    engine,calendar=db
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM ultra_research_bar WHERE trade_date=:day'),{'day':calendar[150].date()})
    with pytest.raises(ValueError,match='预热行情缺失'):
        m.run(engine=engine,strategies='all',output_dir=tmp_path,show_progress=False)


def test_real_git_rename_delete_and_nonancestor(tmp_path):
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=tmp_path,stderr=subprocess.DEVNULL).decode().strip()
    git('init','-q');git('config','user.email','test@example.invalid');git('config','user.name','Test')
    a=tmp_path/'docs/seven-strategy-statistics.md';a.parent.mkdir();a.write_text('research')
    runtime=tmp_path/'runtime.py';runtime.write_text('runtime')
    def commit():
        git('add','.');git('commit','-qm','test');return git('rev-parse','HEAD')
    base=commit()
    a.rename(tmp_path/'docs/ultra-short-statistics.md');renamed=commit()
    assert impact.compare(base,renamed,tmp_path)[0] is False
    (tmp_path/'docs/ultra-short-statistics.md').unlink();deleted=commit()
    assert impact.compare(renamed,deleted,tmp_path)[0] is False
    runtime.rename(a);mixed=commit()
    assert impact.compare(deleted,mixed,tmp_path)[0] is True
    assert impact.compare(base,mixed,tmp_path)[0] is True
    assert impact.compare(mixed,base,tmp_path)[0] is None


def test_manual_deploy_has_no_automatic_comparison_gate():
    workflow=(ROOT/'.github/workflows/deploy.yml').read_text()
    classify=workflow.split('id: impact',1)[1].split('run: python3 scripts/deployment_impact.py',1)[0]
    assert "if: github.event_name == 'push'" in classify
    deploy=workflow.split('  deploy:',1)[1]
    assert "inputs.operation == 'deploy' && vars.DEPLOY_ENABLED == 'true'" in deploy
    assert "inputs.operation == 'check'" in deploy
    assert 'continue-on-error' not in workflow


@pytest.mark.parametrize('path,expected', [
    ('scripts/nested/new.py',False),('scripts/deployment_impact.py',False),
    ('脚本/任意子目录/new.py',False),('scripts-other/tool.py',True),
    ('scripts_backup/tool.py',True),('脚本备份/tool.py',True),
    ('others/scripts/tool.py',True),('script/tool.py',True),
    ('scritps/tool.py',True),('scripts',True),('脚本',True),
])
def test_script_directory_boundaries(path,expected):
    assert impact.requires_deployment([path]) is expected


def test_real_git_script_directories_add_modify_rename_delete_and_mixed(tmp_path):
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=tmp_path,stderr=subprocess.DEVNULL).decode().strip()
    git('init','-q');git('config','user.email','test@example.invalid');git('config','user.name','Test')
    (tmp_path/'README').write_text('base')
    def commit():
        git('add','.');git('commit','-qm','test');return git('rev-parse','HEAD')
    base=commit()
    a=tmp_path/'scripts/nested/deploy.py';a.parent.mkdir(parents=True);a.write_text('new')
    added=commit();assert impact.compare(base,added,tmp_path)[0] is False
    a.write_text('modified');modified=commit();assert impact.compare(added,modified,tmp_path)[0] is False
    b=tmp_path/'脚本/new.py';b.parent.mkdir();a.rename(b)
    renamed=commit();assert impact.compare(modified,renamed,tmp_path)[0] is False
    b.unlink();deleted=commit();assert impact.compare(renamed,deleted,tmp_path)[0] is False
    # A same-prefix lookalike must not inherit the directory exemption.
    similar=tmp_path/'scripts-other/new.py';similar.parent.mkdir();similar.write_text('outside')
    boundary=commit();assert impact.compare(deleted,boundary,tmp_path)[0] is True
    similar.rename(b);crossing=commit();assert impact.compare(boundary,crossing,tmp_path)[0] is True
    (tmp_path/'README').write_text('app change');a.write_text('script change')
    mixed=commit();assert impact.compare(crossing,mixed,tmp_path)[0] is True
    assert impact.compare(base,mixed,tmp_path)[0] is True
