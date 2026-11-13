"""Missing PE stays NULL through arithmetic, legacy storage migration and comparisons."""
from datetime import date, datetime
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace

import pytest
from app.services import valuation_ruler as service
from app.services.valuation_ruler_model import validate_day, ValuationError, ValuationConflict, JQ_SOURCE

DAY = date(2015, 12, 30)
MOMENT = datetime.fromisoformat('2016-01-04T20:00:00+08:00')


def members():
    return {f'{600000+i:06d}.XSHG': None if i == 0 else Decimal('-2') if i == 1 else Decimal('10')
            for i in range(300)}


def monitor(tmp_path):
    return service.ValuationRuler(SimpleNamespace(log_path=tmp_path, valuation_ruler_worker_enabled=False))


def test_missing_pe_separate_from_losses_and_invalid_values():
    values = members()
    point = validate_day(DAY, '4000', values, JQ_SOURCE)
    assert (point['positive_count'], point['loss_count'], point['missing_pe_count']) == (298, 1, 1)
    assert point['mean_pe_ttm'] == '10.000000000000' and point['pe_sum'] == '2980.000000000000'
    for bad in [float('nan'), float('inf'), 'NaN', 0, True, 'invalid']:
        with pytest.raises(ValuationError):
            validate_day(DAY, '4000', dict(values, **{'600000.XSHG': bad}), JQ_SOURCE)
    with pytest.raises(ValuationError):
        validate_day(DAY, '4000', {code: None for code in values}, JQ_SOURCE)
    with pytest.raises(ValuationError):
        validate_day(DAY, '4000', values, JQ_SOURCE, dict(point, missing_pe_count=2))


@pytest.mark.parametrize('candidate_value', [None, Decimal('12')])
def test_null_roundtrip_repeat_and_comparison(tmp_path, candidate_value):
    app = monitor(tmp_path)
    values = members()
    point = validate_day(DAY, '4000', values, JQ_SOURCE)
    with app.db() as db:
        assert app.publish_day(db, point, values, {})
    assert app.history(MOMENT)['latest']['missing_pe_count'] == 1
    with app.db() as db:
        assert db.execute('SELECT COUNT(*) FROM members').fetchone()[0] == 300
        assert db.execute('SELECT pe_ttm FROM members WHERE code=?', ('600000.XSHG',)).fetchone()[0] is None
        assert not app.publish_day(db, point, values, {})
        candidate = dict(values, **{'600000.XSHG': candidate_value})
        daily = validate_day(DAY, '4000', candidate, JQ_SOURCE)
        if candidate_value is None:
            assert not app.publish_day(db, daily, candidate, {})
        else:
            with pytest.raises(ValuationConflict):
                app.publish_day(db, daily, candidate, {})
            report = json.loads(db.execute('SELECT payload FROM comparisons').fetchone()[0])
            assert report['changed_member_count'] == 1
            assert report['changed_members'][0]['published'] is None
            assert report['differences']['missing_pe_count'] == {'published': 1, 'candidate': 0}
    assert app.history(MOMENT)['latest']['missing_pe_count'] == 1


def test_old_sqlite_members_upgraded_without_changing_values_or_payloads(tmp_path):
    root = tmp_path/'market-data'/'valuation-ruler'
    root.mkdir(parents=True)
    values = {code: Decimal('10') for code in members()}
    point = validate_day(DAY, '4000', values, JQ_SOURCE)
    point.pop('missing_pe_count')  # Existing persisted JSON predates the new count.
    with sqlite3.connect(root/'history.sqlite3') as db:
        db.executescript('CREATE TABLE members(trade_date TEXT NOT NULL, code TEXT NOT NULL, '
                         'pe_ttm TEXT NOT NULL, PRIMARY KEY(trade_date,code)); '
                         'CREATE TABLE points(trade_date TEXT PRIMARY KEY,payload TEXT NOT NULL);')
        db.executemany('INSERT INTO members VALUES (?,?,?)', [(str(DAY),code,'10.000000000000') for code in values])
        db.execute('INSERT INTO points VALUES (?,?)', (str(DAY),json.dumps(point)))
    for _ in range(2):
        app = monitor(tmp_path)
        assert app.history(MOMENT)['latest']['missing_pe_count'] == 0
        with app.db() as db:
            assert db.execute('SELECT COUNT(*) FROM members').fetchone()[0] == 300
            assert db.execute('SELECT pe_ttm FROM members LIMIT 1').fetchone()[0] == '10.000000000000'
            assert not app.publish_day(db, validate_day(DAY,'4000',values,JQ_SOURCE), values, {})
            assert db.execute('SELECT COUNT(*) FROM comparisons').fetchone()[0] == 0
            assert json.loads(db.execute('SELECT payload FROM points').fetchone()[0]) == point
            assert not next(row['notnull'] for row in db.execute('PRAGMA table_info(members)') if row['name']=='pe_ttm')


def test_sqlite_upgrade_failure_rolls_back_old_table(tmp_path, monkeypatch):
    root = tmp_path/'market-data'/'valuation-ruler'
    root.mkdir(parents=True)
    path = root/'history.sqlite3'
    original = sqlite3.connect
    with original(path) as db:
        db.execute('CREATE TABLE members(trade_date TEXT NOT NULL,code TEXT NOT NULL,pe_ttm TEXT NOT NULL,PRIMARY KEY(trade_date,code))')
        db.execute('INSERT INTO members VALUES (?,?,?)', (str(DAY),'600000.XSHG','12.5'))
    class BrokenConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('ALTER TABLE members_nullable'):
                raise sqlite3.OperationalError('injected migration failure')
            return super().execute(sql, *args, **kwargs)
    monkeypatch.setattr(service.sqlite3,'connect',lambda *a,**kw:original(*a,**kw,factory=BrokenConnection))
    with pytest.raises(sqlite3.OperationalError):
        monitor(tmp_path)
    with original(path) as db:
        assert db.execute('SELECT * FROM members').fetchall() == [(str(DAY),'600000.XSHG','12.5')]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='members_nullable'").fetchall()
