"""Publish verified aggregates to the existing MySQL tables and result journal."""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select

from app.core.result_sync import HEAD, CHANGE
from app.models.sentiment import MarketSentiment
from app.models.crowding import MarketCrowding
from app.services.etf_collection import WriterFailure, writer_error_code
from app.services.market_methodology import JOINQUANT_HEAT_METHOD


MODELS = {'heat': MarketSentiment, 'crowding': MarketCrowding}
NUMBERS = {'ratio': Decimal('0.0000000001'), 'top_pct': Decimal('0.000001'),
           'top_money': Decimal('0.01'), 'total_money': Decimal('0.01')}


def row_hash(kind, row):
    """Match provenance to actual published values, including the write timestamp."""
    normalized = {}
    for column in MODELS[kind].__table__.columns:
        value = row[column.name] if hasattr(row, '__getitem__') else getattr(row, column.name)
        if column.name in NUMBERS:
            value = format(Decimal(str(value)).quantize(NUMBERS[column.name]), 'f')
        elif isinstance(value, (date, datetime)):
            value = value.isoformat()
        normalized[column.name] = value
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _intent(owner, day, kind, value=None, methodology=None, previous_hash=None, source=None):
    key = f'write-intent:{day.isoformat()}:{kind}'
    with owner.connect() as db:
        if value is not None:
            previous = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            # An earlier source commit may be durable even when the worker's
            # acknowledgement was lost. A later rolled-back attempt must not
            # erase that committed row's ownership proof.
            for value_hash in {value, previous[0] if previous else value, previous_hash} - {None}:
                db.execute('INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)',
                           (f'write-intent-history:{day.isoformat()}:{kind}:{value_hash}', value_hash))
            db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', (key, value))
            if source in ('sina', 'public_market'):
                db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',
                           (f'source:{kind}:{value}', source))
            if methodology is not None:
                # Bind the calculation contract to the exact database row, not
                # just its date. Old observations must never acquire a new label.
                db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',
                           (f'methodology:{kind}:{value}', methodology))
            return value
        row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else None


def _known_intents(owner, day, kind):
    with owner.connect() as db:
        values = {row[0] for row in db.execute('SELECT value FROM metadata WHERE key LIKE ?',
                  (f'write-intent-history:{day.isoformat()}:{kind}:%',))}
        latest = db.execute('SELECT value FROM metadata WHERE key=?',
                            (f'write-intent:{day.isoformat()}:{kind}',)).fetchone()
        if latest:
            values.add(latest[0])
        return values


def _validate_heat(heat):
    if (heat.get('methodology') != JOINQUANT_HEAT_METHOD
            or heat.get('price_basis') != 'qfq' or heat.get('denominator') != 'eligible'):
        raise WriterFailure('writer_methodology_mismatch')
    above, eligible, universe = (heat.get(key) for key in ('above_count', 'eligible_count', 'universe_count'))
    if (any(type(value) is not int for value in (above, eligible, universe))
            or not 0 <= above <= eligible <= universe or eligible <= 0):
        raise WriterFailure('writer_value_mismatch')
    ratio = Decimal(str(heat.get('ratio')))
    expected = (Decimal(above) / Decimal(eligible)).quantize(NUMBERS['ratio'])
    if not ratio.is_finite() or ratio.quantize(NUMBERS['ratio']) != expected:
        raise WriterFailure('writer_value_mismatch')


def _stored_method(owner, kind, value_hash):
    with owner.connect() as db:
        row = db.execute('SELECT value FROM metadata WHERE key=?',
                         (f'methodology:{kind}:{value_hash}',)).fetchone()
        return row[0] if row else None


def _validate_crowding(crowding):
    count, top_count = (crowding.get(key) for key in ('stock_count', 'top_stock_count'))
    if (type(count) is not int or type(top_count) is not int
            or count < 100 or top_count != max(count // 20, 1)):
        raise WriterFailure('writer_value_mismatch')
    top, total, ratio = (Decimal(str(crowding.get(key))) for key in ('top_money', 'total_money', 'ratio'))
    if (not all(value.is_finite() for value in (top, total, ratio))
            or not 0 <= top <= total or total <= 0
            or ratio.quantize(NUMBERS['ratio']) != (top / total).quantize(NUMBERS['ratio'])):
        raise WriterFailure('writer_value_mismatch')


def _journal_head(connection):
    existing = inspect(connection)
    has_head, has_change = existing.has_table(HEAD.name), existing.has_table(CHANGE.name)
    if not has_head and not has_change:
        return None  # Existing legacy readers reconcile the full result table.
    if not has_head or not has_change:
        raise WriterFailure('writer_journal_incomplete')
    row = connection.execute(select(HEAD).where(HEAD.c.module == 'market-sentiment').with_for_update()).mappings().first()
    if row is None:
        connection.execute(HEAD.insert().values(module='market-sentiment', protocol=1,
                                               generation=uuid.uuid4().hex, revision=0))
        row = connection.execute(select(HEAD).where(HEAD.c.module == 'market-sentiment').with_for_update()).mappings().one()
    if row['protocol'] != 1 or row['revision'] < 0 or not row['generation']:
        raise WriterFailure('writer_journal_incomplete')
    return row


def write_market_results(owner, day, heat, crowding, snapshot):
    """Never overwrite a pre-existing record owned by the manual collector."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    config = owner.configuration.load()
    if owner.configuration.issue():
        raise WriterFailure('missing_writer_configuration')
    engine = None
    result = dict(heat_status='pending', crowding_status='pending', heat_hash=None, crowding_hash=None)
    try:
        engine = owner.configuration.engine(config)
        provenance = owner.provenance(day) or {}
        timestamp = datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None, microsecond=0)
        values = {}
        if heat is not None and heat.get('publishable') is True:
            _validate_heat(heat)
            values['heat'] = dict(trade_date=day, ratio=Decimal(str(heat['ratio'])).quantize(NUMBERS['ratio']),
                                  above_count=heat['above_count'], eligible_count=heat['eligible_count'],
                                  universe_count=heat['universe_count'], updated_at=timestamp)
        if crowding is not None and crowding.get('publishable', True):
            _validate_crowding(crowding)
            values['crowding'] = dict(trade_date=day, top_pct=Decimal('0.050000'),
                                      ratio=Decimal(str(crowding['ratio'])).quantize(NUMBERS['ratio']),
                                      top_money=Decimal(str(crowding['top_money'])).quantize(NUMBERS['top_money']),
                                      total_money=Decimal(str(crowding['total_money'])).quantize(NUMBERS['total_money']),
                                      stock_count=crowding['stock_count'], top_stock_count=crowding['top_stock_count'],
                                      updated_at=timestamp)
        with engine.begin() as connection:
            journal = _journal_head(connection) if 'heat' in values else None
            for kind, incoming in values.items():
                table = MODELS[kind].__table__
                condition = table.c.trade_date == day
                if kind == 'crowding':
                    condition &= table.c.top_pct == Decimal('0.050000')
                existing = connection.execute(select(table).where(condition).with_for_update()).mappings().first()
                if existing is not None:
                    actual = row_hash(kind, existing)
                    known = _known_intents(owner, day, kind)
                    if provenance.get(kind + '_hash'):
                        known.add(provenance[kind + '_hash'])
                    if actual not in known:
                        result[kind + '_status'] = 'existing_preserved'
                        continue
                    comparable = incoming | {'updated_at': existing['updated_at']}
                    same_method = kind != 'heat' or _stored_method(owner, kind, actual) == JOINQUANT_HEAT_METHOD
                    if row_hash(kind, comparable) == actual and same_method:
                        result.update({kind + '_status': 'written', kind + '_hash': actual})
                        continue
                expected = row_hash(kind, incoming)
                # Crash recovery: this intent is durable BEFORE the DB commit. A
                # replay recognizes only the exact values that we intended to write.
                _intent(owner, day, kind, expected,
                        JOINQUANT_HEAT_METHOD if kind == 'heat' else None,
                        previous_hash=row_hash(kind, existing) if existing is not None else None,
                        source='public_market' if snapshot.get('source') == 'public_market' else 'sina')
                if existing is None:
                    connection.execute(table.insert().values(**incoming))
                else:
                    connection.execute(table.update().where(condition).values(**incoming))
                if kind == 'heat' and journal is not None:
                    revision = journal['revision'] + 1
                    connection.execute(HEAD.update().where(HEAD.c.module == 'market-sentiment').values(revision=revision))
                    payload = [{'table': table.name, 'key': [day.isoformat()], 'deleted': False}]
                    connection.execute(CHANGE.insert().values(module='market-sentiment', revision=revision,
                                                               payload=json.dumps(payload, separators=(',', ':'))))
                # Verify database precision and retained values before committing.
                written = connection.execute(select(table).where(condition)).mappings().one()
                actual = row_hash(kind, written)
                if actual != expected:
                    raise WriterFailure('writer_value_mismatch')
                result.update({kind + '_status': 'written', kind + '_hash': actual})
        return result
    except WriterFailure:
        raise
    except Exception as error:
        raise WriterFailure(writer_error_code(error)) from error
    finally:
        if engine is not None:
            engine.dispose()


def read_market_dates(owner):
    """Read each latest source date independently; never scan older date gaps.

    A failed read is not an empty table and must never silently reset backfill.
    """
    engine = None
    try:
        engine = owner.configuration.engine(owner.configuration.load())
        with engine.connect() as connection:
            result = {}
            for kind, model in MODELS.items():
                query = select(func.max(model.trade_date))
                if kind == 'crowding':
                    query = query.where(model.top_pct == Decimal('0.050000'))
                latest = connection.execute(query).scalar_one()
                result[kind] = {str(latest)} if latest is not None else set()
            return result
    except Exception as error:
        raise WriterFailure(writer_error_code(error)) from error
    finally:
        if engine is not None:
            engine.dispose()
