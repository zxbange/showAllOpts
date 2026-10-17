"""Test retention offline; never import jqdata, credentials or connect to MySQL."""
import ast
import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, create_engine, func
from sqlalchemy.orm import declarative_base, sessionmaker


@pytest.fixture
def collector(tmp_path):
    source = Path(__file__).resolve().parents[2] / 'collectors' / '行业动量计算_数据库版.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    names = {'IndustryMomentum', 'as_date', 'save_day', 'get_last_saved_date',
             'get_retention_start', 'cleanup_old_data', 'run'}
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    class FrozenDatetime(datetime.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 10, 17, 2)  # Saturday in Beijing.
    namespace = dict(Column=Column, Date=Date, DateTime=DateTime, Integer=Integer,
                     Numeric=Numeric, String=String, func=func, sessionmaker=sessionmaker,
                     Base=declarative_base(), START_DATE='2025-01-01', END_DATE=None,
                     INDUSTRY_TYPE='sw_l2', RETENTION_TRADE_DAYS=180, RESULT_COLUMNS=[],
                     datetime=SimpleNamespace(datetime=FrozenDatetime, date=datetime.date,
                                              timedelta=datetime.timedelta),
                     pd=SimpleNamespace(DataFrame=lambda data=None, columns=None: data or []))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    engine = create_engine('sqlite:///' + str(tmp_path / 'retention.sqlite'))
    namespace['Base'].metadata.create_all(engine)
    namespace['MysqlMomentum'] = SimpleNamespace(connect_to_mysql=lambda: engine)
    days = []
    day = datetime.date(2026, 10, 16)
    while len(days) < 205:
        # A mocked exchange holiday also differs from a weekday-only calendar.
        if day.weekday() < 5 and day != datetime.date(2026, 10, 1):
            days.insert(0, day)
        day -= datetime.timedelta(days=1)
    def calendar(start_date=None, end_date=None, count=None):
        end = namespace['as_date'](end_date)
        available = [d for d in days if d <= end]
        if count is not None:
            return available[-count:]
        start = namespace['as_date'](start_date)
        return [d for d in available if d >= start]
    namespace['get_trade_days'] = calendar
    calculations = []
    def calculate(day, kind):
        calculations.append(day)
        return [record(day, kind)]
    namespace['calculate_momentum'] = calculate
    yield SimpleNamespace(env=namespace, engine=engine, Session=sessionmaker(bind=engine),
                          days=days, cutoff=days[-180], calculated=calculations)
    engine.dispose()


def record(day, kind='sw_l2'):
    return dict(trade_date=day, industry_type=kind, industry_code='fixture',
                industry_name='fixture', momentum=0, momentum_rank=1,
                front_stock_count=0, industry_stock_count=10)


def seed(c, days, kinds=('sw_l2',)):
    with c.Session() as session:
        session.add_all([c.env['IndustryMomentum'](**record(day, kind)) for day in days for kind in kinds])
        session.commit()


def saved(c):
    with c.Session() as session:
        return session.query(c.env['IndustryMomentum'].trade_date,
                             c.env['IndustryMomentum'].industry_type).all()


def test_calendar_uses_180_exchange_days_including_weekend(collector):
    c = collector
    assert c.env['get_retention_start']('2026-10-17') == c.cutoff
    assert (datetime.date(2026, 10, 17) - c.cutoff).days > 180


@pytest.mark.parametrize('invalid', [None, [], [datetime.date(2026, 10, 16)] * 180,
                                    [datetime.date(2026, 10, 18)] * 180])
def test_bad_calendar_stops_before_database_connection(collector, invalid):
    c = collector
    c.env['get_trade_days'] = lambda **kwargs: invalid
    c.env['MysqlMomentum'].connect_to_mysql = lambda: pytest.fail('Must not connect on invalid calendar')
    with pytest.raises(ValueError):
        c.env['run']()


def test_cleanup_keeps_boundary_zero_scores_and_all_classifications(collector):
    c = collector
    seed(c, c.days, ('sw_l1', 'sw_l2'))
    deleted = c.env['cleanup_old_data'](c.Session, c.cutoff)
    assert deleted == (205 - 180) * 2
    rows = saved(c)
    assert len(rows) == 360
    assert min(day for day, kind in rows) == c.cutoff
    assert c.env['cleanup_old_data'](c.Session, c.cutoff) == 0


def test_cleanup_commit_failure_rolls_back(collector):
    c = collector
    seed(c, [c.days[0], c.cutoff])
    class FailingCommit(c.Session.class_):
        def commit(self):
            raise RuntimeError('Injected failure')
    failing_session = sessionmaker(bind=c.engine, class_=FailingCommit)
    with pytest.raises(RuntimeError):
        c.env['cleanup_old_data'](failing_session, c.cutoff)
    assert len(saved(c)) == 2


def test_run_cleans_before_calculation_and_keeps_last_day_refresh(collector):
    c = collector
    seed(c, c.days, ('sw_l1', 'sw_l2'))
    original = c.env['calculate_momentum']
    def calculate(day, kind):
        assert all(saved_day >= c.cutoff for saved_day, _ in saved(c))
        return original(day, kind)
    c.env['calculate_momentum'] = calculate
    c.env['run']()
    assert c.calculated == [c.days[-1]]
    assert len(saved(c)) == 360


@pytest.mark.parametrize('old_records', [True, False])
def test_empty_or_long_stale_database_never_backfills_expired_days(collector, old_records):
    c = collector
    if old_records:
        seed(c, c.days[:5])
    c.env['run']()
    assert c.calculated == c.days[-180:]
    assert len(saved(c)) == 180
    c.env['run']()
    assert len(saved(c)) == 180


def test_explicit_start_within_window_is_honored(collector):
    c = collector
    c.env['run'](start_date=c.days[-3])
    assert c.calculated == c.days[-3:]


def test_historical_end_does_not_shift_retention_or_restore_old_data(collector):
    c = collector
    seed(c, c.days, ('sw_l1', 'sw_l2'))
    c.env['run'](end_date=c.days[0])
    assert not c.calculated
    assert len(saved(c)) == 360


def test_cleanup_failure_stops_run(collector):
    c = collector
    def fail(*args):
        raise RuntimeError('Injected cleanup failure')
    c.env['cleanup_old_data'] = fail
    with pytest.raises(RuntimeError):
        c.env['run']()
    assert not c.calculated


def test_bad_backfill_calendar_cannot_reinsert_expired_data(collector):
    c = collector
    original = c.env['get_trade_days']
    def calendar(**kwargs):
        if 'count' in kwargs:
            return original(**kwargs)
        return [c.days[0], c.days[-1]]
    c.env['get_trade_days'] = calendar
    with pytest.raises(ValueError):
        c.env['run']()
    assert not c.calculated
    assert saved(c) == []


def test_insert_only_skips_latest_date_and_preserves_old_snapshot(collector):
    c=collector
    seed(c, [c.days[-2]])
    result=c.env['run'](skip_existing=True)
    assert len(result)==1 and c.calculated==[c.days[-1]]
    c.calculated.clear()
    assert c.env['run'](skip_existing=True)==[]
    assert c.calculated==[]
    assert len(saved(c))==2
    original=record(c.days[-2])
    changed=dict(original,momentum=99)
    assert c.env['save_day'](c.Session,c.days[-2],'sw_l2',[changed],skip_existing=True)==[]
    with c.Session() as s:
        assert s.query(c.env['IndustryMomentum']).filter_by(trade_date=c.days[-2]).one().momentum==0


def test_insert_only_momentum_defers_intraday_date(collector):
    c=collector
    clock=c.env['datetime'].datetime
    clock.utcnow=classmethod(lambda cls: datetime.datetime(2026,10,16,7))
    first=c.env['run'](skip_existing=True)
    assert first and max(r['trade_date'] for r in first)==c.days[-2]
    c.calculated.clear()
    clock.utcnow=classmethod(lambda cls: datetime.datetime(2026,10,16,8))
    result=c.env['run'](skip_existing=True)
    assert len(result)==1 and c.calculated==[c.days[-1]]
