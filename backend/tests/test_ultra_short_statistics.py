"""合成行情与SQLite验证四策略统计；不连接真实MySQL，不生成真实市场胜率。"""
import datetime as dt
import importlib.util
import io
from pathlib import Path
import threading

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Date, create_engine, event


PATH = Path(__file__).resolve().parents[2] / '脚本/本地统计/七策略胜率统计_本地版.py'
spec = importlib.util.spec_from_file_location('ultra_statistics', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.fixture(autouse=True)
def isolated_local_config(monkeypatch, tmp_path):
    # CLI tests must never load the user's private database settings.
    monkeypatch.setattr(m, 'DEFAULT_CONFIG_PATH', tmp_path / 'no-local-config.json')


def prices(closes, calendar=None, volumes=None):
    closes = np.asarray(closes, dtype=float)
    calendar = pd.bdate_range('2026-01-05', periods=len(closes)) if calendar is None else calendar
    frame = pd.DataFrame(index=calendar)
    for name, values in [('close', closes), ('open', closes-.02),
                         ('high', closes+.05), ('low', closes-.05)]:
        frame[name] = values
        frame['adj_' + name] = values
    frame['volume'] = 100. if volumes is None else volumes
    frame['money'], frame['paused'] = 1e8, 0
    frame['high_limit'] = frame['close'].shift(1).fillna(frame['close'].iloc[0])*1.1
    frame['low_limit'] = frame['close'].shift(1).fillna(frame['close'].iloc[0])*.9
    return frame


def add_identity(frame, code='000001.XSHE'):
    frame = frame.copy()
    frame['trade_date'], frame['code'] = frame.index, code
    return frame.reset_index(drop=True)


def snapshots(calendar, code='000001.XSHE'):
    return pd.DataFrame(dict(trade_date=calendar, code=code, display_name='测试股票',
        listed_date=pd.Timestamp('2020-01-01'), eligible=1, is_st=0, market_cap=100.))


def signal_details(frame, signal_position=0, method=None):
    details = m.forward_outcomes(frame).iloc[[signal_position]].copy()
    details['strategy'] = method or m.STRATEGIES[0]
    details['code'], details['signal_date'] = '000001.XSHE', frame.index[signal_position]
    return details


def test_exact_close_comparison_and_flat_is_not_a_win():
    frame = prices([100, 101, 100, 99])
    result = m.forward_outcomes(frame).iloc[0]
    assert [result['d{}_win'.format(day)] for day in (1, 2, 3)] == [1, 0, 0]
    assert [result['d{}_return_pct'.format(day)] for day in (1, 2, 3)] == pytest.approx([1, 0, -1])
    summary = m.summary_table(signal_details(frame))
    group = summary[summary['策略'] == m.STRATEGIES[0]]
    assert group['胜率(%)'].tolist() == [100, 0, 0]
    assert group['有效样本'].tolist() == [1, 1, 1]
    assert group['持平次数'].tolist() == [0, 1, 0]


def test_horizons_have_independent_pending_denominators():
    frame = prices([100, 101, 102, 103])
    details = pd.concat([signal_details(frame, 0), signal_details(frame, 2)], ignore_index=True)
    group = m.summary_table(details).iloc[:3]
    assert group['有效样本'].tolist() == [2, 1, 1]
    assert group['待观察'].tolist() == [0, 1, 1]
    assert group['胜率(%)'].tolist() == [100, 100, 100]


def test_market_days_skip_weekend_but_not_missing_or_paused_stock_day():
    calendar = pd.to_datetime(['2026-07-03', '2026-07-06', '2026-07-07', '2026-07-08'])
    frame = prices([100, 101, 102, 103], calendar)
    frame.loc[calendar[1], :] = np.nan
    frame.loc[calendar[2], 'paused'] = 1
    result = m.forward_outcomes(frame).iloc[0]
    assert result['d1_date'] == calendar[1]
    assert result['d1_status'] == '缺少行情'
    assert result['d2_status'] == '停牌'
    assert result['d3_status'] == '有效'
    assert result['d3_return_pct'] == pytest.approx(3.)
    assert pd.isna(result['d1_win']) and pd.isna(result['d2_win'])


def test_three_day_any_close_is_distinct_from_third_close():
    frame = prices([100, 101, 99, 98])
    details = signal_details(frame)
    assert m.summary_table(details).iloc[2]['胜率(%)'] == 0
    assert m.window_summary(details).iloc[0]['三日内任一收盘上涨率(%)'] == 100
    frame.loc[frame.index[2], 'paused'] = 1
    window = m.window_summary(signal_details(frame)).iloc[0]
    assert window['完整三日样本'] == 0
    assert pd.isna(window['三日内任一收盘上涨率(%)'])


def test_return_price_choice_is_explicit_and_adjustment_avoids_false_split_loss(monkeypatch):
    frame = prices([100, 50, 51, 52])
    frame['adj_close'] = [100, 100, 102, 104]
    assert m.forward_outcomes(frame).iloc[0]['d1_return_pct'] == 0
    monkeypatch.setattr(m, 'RETURN_PRICE_FIELD', 'close')
    assert m.forward_outcomes(frame).iloc[0]['d1_return_pct'] == -50


def strong_pullback():
    closes = np.linspace(10, 11.6, 90)
    closes[61:71] = np.linspace(11.7, 13.5, 10)
    closes[71:] = [13., 13.2, 13.3] + [13.3]*16
    frame = prices(closes)
    frame.loc[frame.index[70:73], 'volume'] = [200, 40, 80]
    return frame


def test_strong_stock_first_shrinking_pullback():
    frame = strong_pullback()
    result = m.pattern_candidates(frame, m.PARAMS)
    assert result.iloc[72][m.STRATEGIES[0]]
    assert not result.iloc[73][m.STRATEGIES[0]]
    assert result.iloc[72][m.STRATEGIES[0] + '_anchor'] == frame.index[70]
    frame.loc[frame.index[71], 'volume'] = 250
    assert not m.pattern_candidates(frame, m.PARAMS).iloc[72][m.STRATEGIES[0]]


def test_oversold_rebound_requires_close_above_prior_high():
    closes = np.repeat(10., 90)
    closes[68:73] = [9.6, 9.2, 8.9, 9.5, 9.4]
    frame = prices(closes)
    frame.loc[frame.index[71], 'volume'] = 120
    assert m.pattern_candidates(frame, m.PARAMS).iloc[71][m.STRATEGIES[1]]
    frame.loc[frame.index[70], 'adj_high'] = 9.6
    assert not m.pattern_candidates(frame, m.PARAMS).iloc[71][m.STRATEGIES[1]]


def platform_retest():
    closes = np.linspace(9.9, 10., 90)
    closes[70:] = [10.5, 10.12, 10.15] + [10.2]*17
    frame = prices(closes)
    frame.loc[frame.index[70:73], 'volume'] = [200, 70, 70]
    frame.loc[frame.index[71:73], 'adj_low'] = [10.03, 10.04]
    return frame


def test_platform_retest_only_accepts_first_touch():
    frame = platform_retest()
    result = m.pattern_candidates(frame, m.PARAMS)
    assert result.iloc[71][m.STRATEGIES[2]]
    assert not result.iloc[72][m.STRATEGIES[2]]
    frame.loc[frame.index[71], 'adj_low'] = 9.
    result = m.pattern_candidates(frame, m.PARAMS)
    assert not result.iloc[71][m.STRATEGIES[2]]
    assert not result.iloc[72][m.STRATEGIES[2]]


def first_limit_turn():
    closes = np.repeat(10., 90)
    closes[70:] = [11., 10.6, 11., 11.2] + [11.2]*16
    frame = prices(closes)
    frame.loc[frame.index[70:73], 'volume'] = [200, 180, 210]
    frame.loc[frame.index[71], 'adj_high'] = 10.9
    return frame


def test_first_limit_disagreement_turn_and_no_repeat():
    frame = first_limit_turn()
    result = m.pattern_candidates(frame, m.PARAMS)
    assert result.iloc[72][m.STRATEGIES[3]]
    assert not result.iloc[73][m.STRATEGIES[3]]
    frame.loc[frame.index[65], 'high_limit'] = frame.iloc[65]['close']
    assert not m.pattern_candidates(frame, m.PARAMS).iloc[72][m.STRATEGIES[3]]


@pytest.mark.parametrize('problem', ['one_tick_below', 'missing_past_limit'])
def test_first_limit_does_not_infer_board_from_incomplete_or_near_limit_data(problem):
    frame = first_limit_turn()
    if problem == 'one_tick_below':
        frame.loc[frame.index[70], 'close'] -= .01
    else:
        frame.loc[frame.index[65], 'high_limit'] = np.nan
    assert not m.pattern_candidates(frame, m.PARAMS).iloc[72][m.STRATEGIES[3]]


@pytest.mark.parametrize('constructor', [strong_pullback, platform_retest, first_limit_turn])
def test_future_changes_cannot_change_past_strategy_signals(constructor):
    frame = constructor()
    before = m.pattern_candidates(frame, m.PARAMS)
    changed = frame.copy()
    changed.loc[changed.index[75]:, ['adj_open', 'adj_close', 'adj_high', 'adj_low']] *= .5
    changed.loc[changed.index[75]:, 'volume'] *= 10
    after = m.pattern_candidates(changed, m.PARAMS)
    pd.testing.assert_frame_equal(before.iloc[:75], after.iloc[:75])


def test_strength_rank_uses_full_eligible_pool():
    samples = pd.DataFrame({'signal_date': [pd.Timestamp('2026-07-01')]*10,
        'code': [str(i) for i in range(10)], 'ret10': np.arange(10)/100})
    for method in m.STRATEGIES:
        samples[method] = method == m.STRATEGIES[0]
        samples[method + '_anchor'] = pd.Timestamp('2026-06-29')
    details = m.select_signals(samples, m.PARAMS)
    assert set(details['code']) == {'8', '9'}


def test_eligibility_applies_on_signal_day_only():
    frame = strong_pullback()
    calendar = frame.index
    universe = snapshots(calendar)
    universe.loc[universe.index[73]:, ['eligible', 'is_st', 'market_cap']] = [0, 1, 10]
    samples, coverage = m.stock_samples('000001.XSHE', add_identity(frame), universe,
        calendar, calendar[72], calendar[72], m.PARAMS)
    assert len(samples) == 1
    assert samples.iloc[0]['d1_status'] == '有效'  # 事后ST/市值变化不会抹掉后续表现。
    assert samples.iloc[0]['d1_return_pct'] > 0
    assert coverage['base_samples'] == 1
    universe.loc[72, 'is_st'] = 1
    samples, _ = m.stock_samples('000001.XSHE', add_identity(frame), universe,
        calendar, calendar[72], calendar[72], m.PARAMS)
    assert samples.empty


def test_missing_future_bar_is_not_replaced_by_later_stock_row():
    frame = strong_pullback()
    calendar = frame.index
    bars = add_identity(frame).drop(index=73)
    samples, quality = m.stock_samples('000001.XSHE', bars, snapshots(calendar),
        calendar, calendar[72], calendar[72], m.PARAMS)
    assert samples.iloc[0]['d1_date'] == calendar[73]
    assert samples.iloc[0]['d1_status'] == '缺少行情'
    assert samples.iloc[0]['d2_status'] == '有效'
    assert quality['bars_missing_on_snapshot_days'] == 1


@pytest.fixture
def local_destination(tmp_path):
    return tmp_path


def database(engine, frame):
    bars = add_identity(frame)
    bars['trade_date'], bars['asset_type'] = bars['trade_date'].dt.date, 'stock'
    bars.to_sql('ultra_research_bar', engine, index=False, dtype={'trade_date': Date()})
    universe = snapshots(frame.index)
    universe['trade_date'], universe['listed_date'] = universe['trade_date'].dt.date, universe['listed_date'].dt.date
    universe.to_sql('ultra_research_universe', engine, index=False,
                   dtype={'trade_date': Date(), 'listed_date': Date()})
    pd.DataFrame({'trade_date': frame.index.date}).to_sql('ultra_research_calendar', engine,
        index=False, dtype={'trade_date': Date()})


def test_end_to_end_is_read_only_requires_no_index_data_and_exports_all_strategies(local_destination):
    engine = create_engine('sqlite:///:memory:')
    calendar = pd.bdate_range('2025-10-01', periods=250)
    frame = prices(np.linspace(10., 13., len(calendar)), calendar)
    database(engine, frame)  # 数据库没有指数行情，也没有任务表。
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda conn, cursor, sql, *args: statements.append(sql))
    try:
        result = m.run(start_date='2026-07-01', engine=engine, output_dir=local_destination)
        assert len(result['summary']) == 12
        assert set(result['summary']['策略']) == set(m.STRATEGIES)
        assert len(result['windows']) == 4
        assert result['details'].empty
        assert result['summary']['胜率(%)'].isna().all()
        assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
        files = {item.name for item in result['output_dir'].iterdir()}
        assert files == {'收盘胜率汇总.csv', '三日窗口汇总.csv', '策略信号明细.csv',
                         '按月收盘胜率.csv', '数据覆盖.csv', '运行参数.json'}
        assert 'dbpassword' not in (result['output_dir'] / '运行参数.json').read_text(encoding='utf-8')
    finally:
        engine.dispose()


def test_end_to_end_exports_known_signal_and_exact_horizon_wins(local_destination):
    engine = create_engine('sqlite:///:memory:')
    calendar = pd.bdate_range('2025-10-01', periods=250)
    closes = np.linspace(10., 11.6, len(calendar))
    peak = int(calendar.get_loc('2026-07-01')) + 2
    closes[peak-9:peak+1] = np.linspace(11.7, 13.5, 10)
    closes[peak+1:] = 13.3
    closes[peak+1:peak+3] = [13., 13.2]
    frame = prices(closes, calendar)
    frame.loc[calendar[peak:peak+3], 'volume'] = [200, 40, 80]
    database(engine, frame)
    try:
        result = m.run(start_date='2026-07-01', engine=engine, output_dir=local_destination)
        details = result['details']
        details = details[details['strategy'] == m.STRATEGIES[0]]
        assert details['signal_date'].tolist() == [calendar[peak+2]]
        assert details.iloc[0]['signal_close'] == 13.2
        for horizon in (1, 2, 3):
            assert details.iloc[0]['d{}_win'.format(horizon)] == 1
        group = result['summary'][result['summary']['策略'] == m.STRATEGIES[0]]
        assert group['有效样本'].tolist() == [1, 1, 1]
        assert group['胜率(%)'].tolist() == [100., 100., 100.]
        assert len(pd.read_csv(result['output_dir'] / '收盘胜率汇总.csv')) == 12
    finally:
        engine.dispose()


@pytest.mark.parametrize('override,default,required', [(None, True, True), (False, True, False), (True, False, True)])
def test_tls_flag_overrides_config_without_disabling_tls(monkeypatch, override, default, required):
    monkeypatch.setattr(m, 'DB_TLS_VERIFY', default)
    monkeypatch.setattr(m, 'DB_TLS_CA_PEM', '')
    monkeypatch.setattr(m, 'create_engine', lambda url, **kwargs: kwargs)
    options = m.make_engine(dict(host='localhost', dbuser='test', dbpassword='test', dbname='test'), tls_verify=override)
    context = options['connect_args']['ssl']
    assert context.check_hostname is required
    assert context.verify_mode == (m.ssl.CERT_REQUIRED if required else m.ssl.CERT_NONE)
    assert options['connect_args']['connect_timeout'] == 15


@pytest.mark.parametrize('flags,expected', [([], True), (['--tls-verify'], True), (['--no-tls-verify'], False)])
def test_cli_forwards_tls_and_progress_options(monkeypatch, flags, expected):
    captured = []
    monkeypatch.setattr(m, 'run', lambda *args, **kwargs: captured.append((args, kwargs)))
    assert m.main(['--start', '2026-07-01', '--no-progress']+flags) == 0
    assert captured[0][1]['tls_verify'] is expected
    assert not captured[0][1]['show_progress']
    assert captured[0][0][0] == '2026-07-01'


def test_certificate_error_explains_ca_and_cli_switch_without_exposing_password(monkeypatch, capsys):
    secret = 'fake@secret:/word'
    monkeypatch.setattr(m, 'DB_CONFIG', {'dbpassword': secret})
    def fail(*args, **kwargs):
        raise RuntimeError('CERTIFICATE_VERIFY_FAILED certificate verify failed ' + m.quote_plus(secret, safe=''))
    monkeypatch.setattr(m, 'run', fail)
    assert m.main([]) == 1
    output = capsys.readouterr().err
    assert 'DB_TLS_CA_PEM' in output and '--no-tls-verify' in output
    assert '不验证服务器身份' in output
    assert secret not in output and m.quote_plus(secret, safe='') not in output


def test_progress_refreshes_while_database_waits_and_stops_cleanly():
    stream = io.StringIO()
    with m.StatisticsProgress(stream=stream, interval=.01) as progress:
        progress.phase('连接数据库/读取日历')
        threading.Event().wait(.06)
    assert stream.getvalue().count('\r') >= 3
    assert '处理中' in stream.getvalue()
    assert not progress.thread.is_alive()
    assert stream.getvalue().endswith('\n')


def test_progress_count_is_actual_completion_and_failure_does_not_show_full_count():
    stream = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        with m.StatisticsProgress(stream=stream) as progress:
            progress.phase('计算策略', total=10)
            progress.update(done=4, detail='000001.XSHE')
            raise KeyboardInterrupt()
    output = stream.getvalue()
    assert '4/10 40.0%' in output
    assert '失败/中断' in output
    assert '10/10' not in output
    assert not progress.thread.is_alive()


def test_disabled_progress_has_no_refresh_thread_or_bar():
    stream = io.StringIO()
    with m.StatisticsProgress(enabled=False, stream=stream) as progress:
        progress.phase('计算策略', total=10)
        progress.update(done=4)
    assert progress.thread is None
    assert stream.getvalue() == ''


def test_failed_initial_database_query_disposes_owned_engine(monkeypatch):
    disposed = []
    class Engine:
        def dispose(self):
            disposed.append(True)
    monkeypatch.setattr(m, 'make_engine', lambda *a, **kw: Engine())
    def fail(*args, **kwargs):
        raise RuntimeError('certificate verify failed')
    monkeypatch.setattr(m, 'validate_schema', fail)
    with pytest.raises(RuntimeError, match='certificate verify failed'):
        m.run(show_progress=False)
    assert disposed == [True]


def local_settings():
    return {'DB_CONFIG': dict(host='example.invalid', port=3306, dbname='research',
                              dbuser='reader', dbpassword='fake@secret:/word'),
            'DB_TLS_VERIFY': False, 'DB_TLS_CA_PEM': 'fake-ca'}


def test_main_loads_private_config_and_cli_can_override_tls(tmp_path, monkeypatch):
    settings = local_settings()
    config = tmp_path / 'settings.json'
    config.write_text(m.json.dumps(settings), encoding='utf-8')
    monkeypatch.setattr(m, 'DEFAULT_CONFIG_PATH', config)
    called = []
    monkeypatch.setattr(m, 'run', lambda *args, **kwargs: called.append(kwargs))
    before = dict(m.DB_CONFIG)
    assert m.main(['--no-progress']) == 0
    assert called[-1]['db_config'] == settings['DB_CONFIG']
    assert called[-1]['tls_verify'] is False
    assert called[-1]['tls_ca_pem'] == 'fake-ca'
    assert m.main(['--tls-verify', '--config', str(config)]) == 0
    assert called[-1]['tls_verify'] is True
    assert m.DB_CONFIG == before and m.DB_TLS_VERIFY is True


def test_config_default_fallback_and_explicit_missing_are_distinct():
    result = m.load_local_config()
    assert result['DB_CONFIG'] == m.DB_CONFIG and result['DB_CONFIG'] is not m.DB_CONFIG
    assert result['DB_TLS_VERIFY'] == m.DB_TLS_VERIFY
    with pytest.raises(ValueError, match='不存在'):
        m.load_local_config('missing-explicit.json')


def test_config_paths_use_script_location_not_working_directory(tmp_path, monkeypatch):
    location = tmp_path / 'script'
    location.mkdir()
    cwd = tmp_path / 'other'
    cwd.mkdir()
    config = location / 'settings.json'
    settings = local_settings()
    config.write_text(m.json.dumps(settings), encoding='utf-8')
    monkeypatch.setattr(m, '__file__', str(location / 'statistics.py'))
    monkeypatch.setattr(m, 'DEFAULT_CONFIG_PATH', config)
    monkeypatch.chdir(cwd)
    assert m.load_local_config() == settings
    assert m.load_local_config('settings.json') == settings


@pytest.mark.parametrize('bad_config', [
    '{"DB_CONFIG":{"dbpassword":"fake-secret"},',
    '"fake-secret"',
    '{"DB_CONFIG":{"dbpassword":"fake-secret"}}',
    '{"unexpected":"fake-secret"}',
])
def test_invalid_config_never_exposes_its_content(tmp_path, monkeypatch, capsys, bad_config):
    config = tmp_path / 'invalid.json'
    config.write_text(bad_config, encoding='utf-8')
    monkeypatch.setattr(m, 'run', lambda *args, **kwargs: pytest.fail('Invalid config must not start a query'))
    assert m.main(['--config', str(config)]) == 1
    output = capsys.readouterr().err
    assert '本机配置' in output and 'fake-secret' not in output


def test_loaded_config_password_is_redacted_from_connection_errors(tmp_path, monkeypatch, capsys):
    settings = local_settings()
    config = tmp_path / 'settings.json'
    config.write_text(m.json.dumps(settings), encoding='utf-8')
    password = settings['DB_CONFIG']['dbpassword']
    def fail(*args, **kwargs):
        raise RuntimeError(password + ' ' + m.quote_plus(password, safe=''))
    monkeypatch.setattr(m, 'run', fail)
    assert m.main(['--config', str(config)]) == 1
    output = capsys.readouterr().err
    assert password not in output and m.quote_plus(password, safe='') not in output
    assert '***' in output


def test_import_does_not_read_local_private_configuration(monkeypatch):
    previous = Path.read_text
    def guarded_read(path, *args, **kwargs):
        if path.suffix == '.json':
            pytest.fail('Import must not read private configuration')
        return previous(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', guarded_read)
    fresh_spec = importlib.util.spec_from_file_location('ultra_statistics_import_only', PATH)
    fresh = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    assert callable(fresh.run)
