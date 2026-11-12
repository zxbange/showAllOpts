from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select

from app.core.result_sync import META, HEAD, CHANGE
from app.models.sentiment import MarketSentiment
from app.models.crowding import MarketCrowding
from app.services.market_writer import write_market_results, row_hash
from app.services.etf_collection import WriterFailure
from app.services.market_methodology import JOINQUANT_HEAT_METHOD


DAY = date(2026, 11, 10)
HEAT = dict(publishable=True, ratio='0.1', above_count=2, eligible_count=20, universe_count=100,
            methodology=JOINQUANT_HEAT_METHOD, price_basis='qfq', denominator='eligible')
CROWDING = dict(ratio='0.4', top_money='400', total_money='1000', stock_count=100, top_stock_count=5)


class Owner:
    def __init__(self, tmp_path):
        self.engine = create_engine('sqlite:///' + str(tmp_path / 'business.sqlite'))
        for table in (MarketSentiment.__table__, MarketCrowding.__table__):
            table.create(self.engine)
        self.state = tmp_path / 'state.sqlite'
        self.hashes = {}
        self.configuration = SimpleNamespace(load=lambda: {}, issue=lambda: None, engine=lambda _: self.engine)
        with self.connect() as db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.state)
        try:
            with db:
                yield db
        finally:
            db.close()

    def provenance(self, day):
        return self.hashes


def test_write_to_existing_tables_replay_recovers_commit_without_local_ack(tmp_path):
    owner = Owner(tmp_path)
    META.create_all(owner.engine)
    first = write_market_results(owner, DAY, HEAT, CROWDING, {})
    assert first['heat_status'] == first['crowding_status'] == 'written'
    # Simulate a crash after MySQL commit and before local completion/hash bookkeeping.
    second = write_market_results(owner, DAY, HEAT, CROWDING, {})
    assert first['changed_count'] == 2 and second['changed_count'] == 0
    assert {k:v for k,v in first.items() if not k.startswith('changed_')} == {k:v for k,v in second.items() if not k.startswith('changed_')}
    with owner.engine.connect() as connection:
        heat = connection.execute(select(MarketSentiment.__table__)).mappings().one()
        assert heat['ratio'] == Decimal('0.1') and heat['eligible_count'] == 20 and heat['universe_count'] == 100
        assert row_hash('heat', heat) == first['heat_hash']
        assert connection.execute(select(HEAD.c.revision)).scalar_one() == 1
        assert len(connection.execute(select(CHANGE)).all()) == 1
    with owner.connect() as db:
        assert db.execute('SELECT value FROM metadata WHERE key=?',
                          ('methodology:heat:' + first['heat_hash'],)).fetchone()[0] == JOINQUANT_HEAT_METHOD


def test_existing_manual_records_are_preserved_and_not_claimed(tmp_path):
    owner = Owner(tmp_path)
    with owner.engine.begin() as connection:
        connection.execute(MarketSentiment.__table__.insert().values(trade_date=DAY, ratio='.5',
            above_count=10, eligible_count=20, universe_count=50, updated_at=datetime(2026, 11, 10, 8)))
    result = write_market_results(owner, DAY, HEAT, CROWDING, {})
    assert result['heat_status'] == 'existing_preserved' and result['heat_hash'] is None
    assert result['crowding_status'] == 'written'
    with owner.engine.connect() as connection:
        assert connection.execute(select(MarketSentiment.ratio)).scalar_one() == Decimal('.5')


def test_source_change_and_journal_commit_are_atomic(tmp_path):
    owner = Owner(tmp_path)
    META.create_all(owner.engine)
    with owner.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_journal BEFORE INSERT ON result_sync_change BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(WriterFailure):
        write_market_results(owner, DAY, HEAT, CROWDING, {})
    with owner.engine.connect() as connection:
        assert not connection.execute(select(MarketSentiment.__table__)).all()
        assert not connection.execute(select(MarketCrowding.__table__)).all()
    with owner.engine.begin() as connection:
        connection.exec_driver_sql('DROP TRIGGER reject_journal')
    assert write_market_results(owner, DAY, HEAT, CROWDING, {})['heat_status'] == 'written'


def test_unacknowledged_commit_remains_owned_when_a_later_attempt_rolls_back(tmp_path):
    owner = Owner(tmp_path)
    META.create_all(owner.engine)
    first = write_market_results(owner, DAY, HEAT, None, {})
    # A committed, but not acknowledged in days, then a different valid result B.
    changed = HEAT | {'above_count': 3, 'ratio': '.15'}
    with owner.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_journal BEFORE INSERT ON result_sync_change BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(WriterFailure):
        write_market_results(owner, DAY, changed, None, {})
    with owner.engine.connect() as connection:
        actual = connection.execute(select(MarketSentiment.__table__)).mappings().one()
        assert row_hash('heat', actual) == first['heat_hash']
    with owner.connect() as db:
        saved = {row[0] for row in db.execute("SELECT value FROM metadata WHERE key LIKE 'write-intent-history:%'")}
        assert first['heat_hash'] in saved and len(saved) == 2
    with owner.engine.begin() as connection:
        connection.exec_driver_sql('DROP TRIGGER reject_journal')
    assert write_market_results(owner, DAY, changed, None, {})['heat_status'] == 'written'
    with owner.engine.connect() as connection:
        assert connection.execute(select(MarketSentiment.ratio)).scalar_one() == Decimal('.15')


def test_outside_edit_invalidates_ownership(tmp_path):
    owner = Owner(tmp_path)
    owner.hashes = write_market_results(owner, DAY, HEAT, CROWDING, {})
    with owner.engine.begin() as connection:
        connection.execute(MarketSentiment.__table__.update().values(ratio='.6', above_count=12))
    result = write_market_results(owner, DAY, HEAT, CROWDING, {})
    assert result['heat_status'] == 'existing_preserved'
    assert result['heat_hash'] is None


def test_partial_heat_does_not_publish_zero_or_replace_crowding(tmp_path):
    owner = Owner(tmp_path)
    result = write_market_results(owner, DAY, HEAT | {'publishable': False, 'ratio': None}, CROWDING, {})
    assert result['heat_status'] == 'pending' and result['crowding_status'] == 'written'
    with owner.engine.connect() as connection:
        assert not connection.execute(select(MarketSentiment.__table__)).all()


def test_incomplete_journal_blocks_new_heat(tmp_path):
    owner = Owner(tmp_path)
    HEAD.create(owner.engine)
    with pytest.raises(WriterFailure, match='writer_journal_incomplete'):
        write_market_results(owner, DAY, HEAT, None, {})


@pytest.mark.parametrize('change', [
    {'methodology': 'sina_bfq_ma20_full_universe_v1'},
    {'price_basis': 'bfq'}, {'denominator': 'universe'},
])
def test_writer_rejects_non_joinquant_heat_contract(tmp_path, change):
    owner = Owner(tmp_path)
    with pytest.raises(WriterFailure, match='writer_methodology_mismatch'):
        write_market_results(owner, DAY, HEAT | change, None, {})
    with owner.engine.connect() as connection:
        assert not connection.execute(select(MarketSentiment.__table__)).all()


@pytest.mark.parametrize('change', [
    {'ratio': '.02'}, {'eligible_count': 0}, {'above_count': 21}, {'universe_count': 19},
])
def test_writer_rejects_inconsistent_counts_and_total_universe_ratio(tmp_path, change):
    owner = Owner(tmp_path)
    with pytest.raises(WriterFailure, match='writer_value_mismatch'):
        write_market_results(owner, DAY, HEAT | change, None, {})


@pytest.mark.parametrize('change', [
    {'stock_count': 99, 'top_stock_count': 4}, {'top_stock_count': 6}, {'ratio': '.5'},
])
def test_writer_checks_original_crowding_contract(tmp_path, change):
    owner = Owner(tmp_path)
    with pytest.raises(WriterFailure, match='writer_value_mismatch'):
        write_market_results(owner, DAY, None, CROWDING | change, {})


def test_read_market_dates_independent_and_filters_crowding_fraction(tmp_path):
    from app.services.market_writer import read_market_dates
    owner = Owner(tmp_path)
    write_market_results(owner, DAY, HEAT, None, {})
    write_market_results(owner, date(2026, 11, 6), HEAT, CROWDING, {})
    write_market_results(owner, date(2026, 11, 9), None, CROWDING, {})
    with owner.engine.begin() as connection:
        connection.execute(MarketCrowding.__table__.insert().values(
            trade_date=DAY, top_pct='.1', ratio='.4', top_money='400', total_money='1000',
            stock_count=100, top_stock_count=10, updated_at=datetime(2026, 11, 10, 18)))
    assert read_market_dates(owner) == {'heat': {'2026-11-10'}, 'crowding': {'2026-11-09'}}


def test_public_source_is_bound_to_written_row_hash(tmp_path):
    owner = Owner(tmp_path)
    result = write_market_results(owner, DAY, HEAT, CROWDING, {'source': 'public_market'})
    with owner.connect() as db:
        for kind in ('heat', 'crowding'):
            assert db.execute('SELECT value FROM metadata WHERE key=?',
                              (f'source:{kind}:{result[kind + "_hash"]}',)).fetchone()[0] == 'public_market'


@pytest.mark.parametrize('operation', ['read', 'write'])
def test_writer_preserves_safe_driver_diagnostics_without_exposing_message(tmp_path, operation):
    from pymysql.err import OperationalError
    from app.core.diagnostics import diagnostic_details
    from app.services.market_writer import read_market_dates
    owner = Owner(tmp_path)
    def unavailable(config):
        raise OperationalError(1045, 'password=privatewritersecret host=private.invalid')
    owner.configuration.engine = unavailable
    with pytest.raises(WriterFailure) as caught:
        if operation == 'read':
            read_market_dates(owner)
        else:
            write_market_results(owner, DAY, HEAT, CROWDING, {})
    details = diagnostic_details(caught.value)
    assert 'mysql_errno=1045' in details
    assert ':unavailable' in details
    assert caught.value.__cause__ is not None
    assert 'privatewritersecret' not in str(caught.value) + details
    assert 'private.invalid' not in str(caught.value) + details
