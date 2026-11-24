"""Report facts come from verified publications and share trigger-independent structure."""
from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.crowding import MarketCrowding
from app.models.sentiment import MarketSentiment
from app.services import collection_notifications as notifications
from test_dividend_collection import CHINA, DividendCollection, result
from test_market_collection import EVENING, worker


def test_market_alert_reads_bounded_page_publications_not_working_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, 'enabled', lambda *_: True)
    monkeypatch.setattr(notifications, 'start_notifications', lambda *_: None)
    monitor, _, _, clock = worker(tmp_path, refresh=lambda: True)
    engine = create_engine('sqlite://')
    tables = MarketSentiment.__table__, MarketCrowding.__table__
    for table in tables:
        table.create(engine)
    with engine.begin() as conn:
        for day, ratio in [(9, '.42'), (10, '.45'), (11, '.99')]:
            stamped = dict(trade_date=date(2026, 11, day), ratio=Decimal(ratio), updated_at=datetime(2026, 11, day))
            conn.execute(tables[0].insert().values(**stamped, above_count=45, eligible_count=100, universe_count=105))
            conn.execute(tables[1].insert().values(**stamped, top_pct=Decimal('.05'), top_money=125, total_money=300,
                                                    stock_count=100, top_stock_count=5))
            conn.execute(tables[1].insert().values(**stamped, top_pct=Decimal('.10'), top_money=999, total_money=1000,
                                                    stock_count=100, top_stock_count=10))
    monitor.notification_cache = SimpleNamespace(session=lambda _: Session(engine))
    assert monitor.tick(EVENING)
    assert monitor.request_run('manual-after-scheduled')['status'] == 'queued'
    monitor.tick(clock.advance(minutes=1))
    with notifications._database(monitor.settings) as db:
        auto, manual = [row['message'] for row in db.execute('SELECT * FROM outbox ORDER BY rowid')]
    for message in (auto, manual):
        assert '目标数据日期：2026-11-10' in message
        assert '市场热度：45%' in message and '变动 +3个百分点' in message
        assert '抱团拥挤度：45%' in message
        assert '成交额 125 / 300 元' in message
        assert '99%' not in message and '999 / 1000' not in message
    assert '触发方式：自动采集' in auto and '触发方式：手动采集' in manual


def test_scheduled_dividend_no_change_and_later_failure_keep_verified_details(tmp_path, monkeypatch):
    from app.services import dividend_collection as module
    monkeypatch.setattr(notifications, 'enabled', lambda *_: True)
    monkeypatch.setattr(notifications, 'start_notifications', lambda *_: None)
    config = {'revision': 1, 'host': 'localhost', 'port': 3306, 'database': 'test'}
    monkeypatch.setattr(module, 'database_config', lambda _: config)
    engine = create_engine('sqlite:///' + str(tmp_path / 'dividend-results.sqlite'))

    class Source:
        def __init__(self, *_, as_of, **__):
            self.as_of = as_of
        def collect(self):
            if self.as_of.day == 17:
                raise RuntimeError('PRIVATE-DIVIDEND-RAW-FAILURE')
            return result()

    monitor = DividendCollection(SimpleNamespace(log_path=tmp_path), source_factory=Source,
        engine_factory=lambda *_, **__: engine, client_factory=lambda: nullcontext(None))
    monkeypatch.setattr(monitor, 'sync_local', lambda *_, **__: None)
    moment = datetime(2026, 11, 16, 23, tzinfo=CHINA)
    assert monitor.refresh(background=False, moment=moment)
    assert monitor.refresh(background=False, moment=moment, slot='2026-11-16')
    assert not monitor.refresh(background=False, moment=moment, slot='2026-11-16')
    assert monitor.refresh(background=False, moment=moment.replace(day=17), slot='2026-11-17')
    with notifications._database(monitor.settings) as db:
        manual, auto, failed = [row['message'] for row in db.execute('SELECT * FROM outbox ORDER BY rowid')]
    for message in (manual, auto, failed):
        assert '2026-11-16' in message and '非银行' in message and '银行' in message
        assert 'PRIVATE-DIVIDEND' not in message
    assert '触发方式：自动采集' in auto and '成功（无新增或变更）' in auto
    assert '触发方式：手动采集' in manual
    assert '目标数据日期：2026-11-17' in failed and '采集结果：失败' in failed
