from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.sentiment import MarketSentiment
from app.models.crowding import MarketCrowding
from app.services.sentiment import SentimentService
from app.services.crowding import CrowdingService
from app.services.market_writer import row_hash
from app.services.market_provenance import history_provenance
from app.services.market_methodology import JOINQUANT_HEAT_METHOD


def test_legacy_total_universe_metadata_is_still_read_without_relabelling():
    rows = [MarketSentiment(trade_date=date(2026, 11, 9), ratio=Decimal('.25'), above_count=1,
                           eligible_count=4, universe_count=10, updated_at=datetime(2026, 11, 9)),
            MarketSentiment(trade_date=date(2026, 11, 10), ratio=Decimal('.1'), above_count=1,
                           eligible_count=4, universe_count=10, updated_at=datetime(2026, 11, 10))]
    def provenance(row):
        return {'source': 'sina', 'price_basis': 'bfq', 'denominator': 'universe'} if row.trade_date.day == 10 else {}
    service = SentimentService(SimpleNamespace(history=lambda end: rows), provenance)
    result = service.history(0, rows[-1].trade_date)
    assert [point.state for point in result.points] == ['normal', 'cold']
    assert [point.denominator for point in result.points] == ['eligible', 'universe']


def test_crowding_ema_restarts_at_source_boundary_before_display_slice():
    rows = [MarketCrowding(top_pct=Decimal('.05'), trade_date=date(2026, 11, day), ratio=Decimal(value),
                           top_money=100, total_money=200, stock_count=50, top_stock_count=2,
                           updated_at=datetime(2026, 11, day)) for day, value in [(7, '.9'), (8, '.8'), (9, '.2'), (10, '.4')]]
    service = CrowdingService(SimpleNamespace(history=lambda end: rows),
                             lambda row: {'source': 'sina' if row.trade_date.day >= 9 else 'joinquant'})
    result = service.history(0, date(2026, 11, 10))
    assert result.points[2].smoothed_ratio == .2
    assert abs(result.points[3].smoothed_ratio - (.4 + (9 / 11) * .2) / (1 + 9 / 11)) < 1e-12
    assert result.latest.source == 'sina'


def test_hash_covers_updated_at_and_all_counts():
    row = dict(trade_date=date(2026, 11, 10), ratio=Decimal('.1'), above_count=1, eligible_count=4,
               universe_count=10, updated_at=datetime(2026, 11, 10))
    expected = row_hash('heat', row)
    assert row_hash('heat', row | {'ratio': .1}) == expected
    assert row_hash('heat', row | {'eligible_count': 5}) != expected
    assert row_hash('heat', row | {'updated_at': datetime(2026, 11, 10, 1)}) != expected


@pytest.mark.parametrize('methodology,expected', [
    (None, {'source': 'sina', 'price_basis': 'bfq', 'denominator': 'universe'}),
    (JOINQUANT_HEAT_METHOD, {'source': 'sina', 'price_basis': 'qfq', 'denominator': 'eligible'}),
])
def test_committed_intent_identifies_source_and_exact_method_before_ack(tmp_path, methodology, expected):
    from contextlib import contextmanager
    import sqlite3
    row = MarketSentiment(trade_date=date(2026, 11, 10), ratio=Decimal('.1'), above_count=1,
                          eligible_count=4, universe_count=10, updated_at=datetime(2026, 11, 10))
    @contextmanager
    def connect():
        db = sqlite3.connect(tmp_path / 'state.sqlite')
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
    with connect() as db:
        db.execute('CREATE TABLE days(trade_date TEXT,heat_hash TEXT,crowding_hash TEXT)')
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO days VALUES(?,NULL,NULL)', ('2026-11-10',))
        db.execute('INSERT INTO metadata VALUES(?,?)', ('write-intent:2026-11-10:heat', row_hash('heat', row)))
        if methodology:
            db.execute('INSERT INTO metadata VALUES(?,?)', ('methodology:heat:' + row_hash('heat', row), methodology))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(market_collection=SimpleNamespace(root=tmp_path, connect=connect))))
    metadata = history_provenance(request, 'heat')
    assert metadata(row) == expected
    row.above_count = 2
    assert metadata(row)['source'] == 'joinquant'


def test_previous_cached_sina_row_keeps_its_method_after_new_source_commit(tmp_path):
    from contextlib import contextmanager
    import sqlite3
    old_row = MarketSentiment(trade_date=date(2026, 11, 10), ratio=Decimal('.45'), above_count=45,
                              eligible_count=50, universe_count=100, updated_at=datetime(2026, 11, 10))
    new_row = MarketSentiment(trade_date=date(2026, 11, 10), ratio=Decimal('.9'), above_count=45,
                              eligible_count=50, universe_count=100, updated_at=datetime(2026, 11, 10, 1))
    old_hash, new_hash = row_hash('heat', old_row), row_hash('heat', new_row)
    @contextmanager
    def connect():
        db = sqlite3.connect(tmp_path / 'state.sqlite')
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
    with connect() as db:
        db.execute('CREATE TABLE days(trade_date TEXT,heat_hash TEXT,crowding_hash TEXT)')
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO days VALUES(?,?,NULL)', ('2026-11-10', new_hash))
        db.executemany('INSERT INTO metadata VALUES(?,?)', [
            ('write-intent:2026-11-10:heat', new_hash),
            ('write-intent-history:2026-11-10:heat:' + old_hash, old_hash),
            ('methodology:heat:' + new_hash, JOINQUANT_HEAT_METHOD),
        ])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(market_collection=SimpleNamespace(root=tmp_path, connect=connect))))
    metadata = history_provenance(request, 'heat')
    assert metadata(old_row) == {'source': 'sina', 'price_basis': 'bfq', 'denominator': 'universe'}
    assert metadata(new_row) == {'source': 'sina', 'price_basis': 'qfq', 'denominator': 'eligible'}
    result = SentimentService(SimpleNamespace(history=lambda end: [old_row]), metadata).history(0, old_row.trade_date)
    assert result.latest.state == 'normal'
