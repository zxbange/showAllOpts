"""V2 calculated index summaries: bounded source reads, no member-history download."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re

from sqlalchemy import MetaData, Table, inspect, select

from app.core.cache_errors import SnapshotDataError

METHOD = 'index_pe_lyr_ttm_cap300_v2'
SOURCE = 'JQ_PLATFORM.VALUATION.V2'
TABLE = 'valuation_ruler_daily_v2'
INDICES = {'000300.XSHG': ('沪深300', 300, '2010-01-01'),
           '000016.XSHG': ('上证50', 50, '2010-01-01'),
           '000852.XSHG': ('中证1000', 1000, '2014-10-17')}
METRICS = tuple(metric+'_pe_'+basis for basis in ('ttm', 'lyr') for metric in ('overall', 'mean', 'median'))


def init_store(db):
    db.execute('CREATE TABLE IF NOT EXISTS index_points_v2 '
               '(index_code TEXT NOT NULL,trade_date TEXT NOT NULL,payload TEXT NOT NULL, '
               'PRIMARY KEY(index_code,trade_date))')


def available(engine):
    return inspect(engine).has_table(TABLE)


def checked(row, cutoff):
    """Validate the collector contract; raw-input verification remains a source audit."""
    def reject():
        raise SnapshotDataError('指数估值v2汇总不完整或计算口径不匹配；已有缓存保留。')
    code, day = row['index_code'], row['trade_date']
    if (code not in INDICES or type(day) is not date or not date.fromisoformat(INDICES[code][2]) <= day <= cutoff
            or day.weekday() >= 5 or row['method_version'] != METHOD or row['source'] != SOURCE
            or row['member_count'] != INDICES[code][1] or not isinstance(row['updated_at'], datetime)
            or not re.fullmatch('[a-f0-9]{64}', row['members_sha256'])):
        reject()
    try:
        stats = json.loads(row['statistics'])
        if (stats['profit_basis'] != 'market_cap_divided_by_pe' or stats['market_cap_unit'] != '亿元'
                or stats['ordinary_trading_suspension_excluded'] is not False
                or stats['listing_status_basis'] not in ('historical_index_members', 'jy_listing_history')):
            reject()
        result = {k: row[k] for k in ('index_code', 'method_version', 'member_count', 'members_sha256', 'source')}
        result.update(trade_date=str(day), updated_at=row['updated_at'].isoformat(), statistics=stats)
        for field in ('index_close',)+METRICS:
            value = row[field]
            if value is None:
                if field == 'index_close': reject()
                result[field] = None
                continue
            value = Decimal(str(value))
            if not value.is_finite() or value <= 0 or value >= Decimal('1e18'):
                reject()
            if field.startswith(('mean_', 'median_')) and value > 300:
                reject()
            result[field] = str(value)
        for basis in ('ttm', 'lyr'):
            counts = stats[basis]
            fields = ('eligible', 'loss', 'over_limit', 'missing_pe', 'suspended_listing', 'overall_valid', 'overall_missing')
            if any(type(counts[k]) is not int or not 0 <= counts[k] <= row['member_count'] for k in fields):
                reject()
            if sum(counts[k] for k in fields[:5]) != row['member_count']:
                reject()
            if counts['overall_valid']+counts['overall_missing']+counts['suspended_listing'] != row['member_count']:
                reject()
            if any((result[m+'_pe_'+basis] is None) != (counts['eligible'] == 0) for m in ('mean', 'median')):
                reject()
            if counts['overall_missing'] and result['overall_pe_'+basis] is not None:
                reject()
        return result
    except (KeyError, TypeError, ValueError, InvalidOperation):
        reject()


def sync(engine, service, cutoff, check, progress, *, full=False, same_source=True):
    """Runs against copy_history's private SQLite snapshot; failure cannot publish it."""
    table = Table(TABLE, MetaData(), autoload_with=engine)
    inserted = updated = processed = 0
    with service.db() as local:
        init_store(local)
        if not same_source:
            local.execute('DELETE FROM index_points_v2')
        for index, code in enumerate(INDICES):
            recent = local.execute('SELECT trade_date FROM index_points_v2 WHERE index_code=? '
                                   'ORDER BY trade_date DESC LIMIT 3', (code,)).fetchall()
            start = date.fromisoformat(recent[-1][0]) if recent and not full else date.fromisoformat(INDICES[code][2])
            after = None
            while True:
                check()
                progress('{}：同步日汇总，已处理 {} 条'.format(INDICES[code][0], processed), 5+index*28)
                query = select(table).where(table.c.index_code == code, table.c.method_version == METHOD,
                    table.c.trade_date >= start, table.c.trade_date <= cutoff).order_by(table.c.trade_date).limit(500)
                if after is not None:
                    query = query.where(table.c.trade_date > after)
                # Release the source connection before local processing/writes.
                with engine.connect() as connection:
                    rows = connection.execute(query).mappings().all()
                for row in rows:
                    check()
                    point = checked(row, cutoff)
                    payload = json.dumps(point, ensure_ascii=False, sort_keys=True)
                    prior = local.execute('SELECT payload FROM index_points_v2 WHERE index_code=? AND trade_date=?',
                                          (code, point['trade_date'])).fetchone()
                    if prior is None:
                        local.execute('INSERT INTO index_points_v2 VALUES (?,?,?)', (code, point['trade_date'], payload))
                        inserted += 1
                    elif prior[0] != payload:
                        local.execute('UPDATE index_points_v2 SET payload=? WHERE index_code=? AND trade_date=?',
                                      (payload, code, point['trade_date']))
                        updated += 1
                    processed += 1
                if len(rows) < 500:
                    break
                after = rows[-1]['trade_date']
    return dict(inserted=inserted, updated=updated, processed=processed)


def history(service, cutoff, index_code, basis):
    from app.services.valuation_ruler import quantile
    with service.db() as db:
        init_store(db)
        present = db.execute('SELECT 1 FROM index_points_v2 LIMIT 1').fetchone()
        if not present and index_code == '000300.XSHG' and basis == 'ttm':
            return None  # Retain the existing v1 view until new data has been synced.
        rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM index_points_v2 '
            'WHERE index_code=? AND trade_date<=? ORDER BY trade_date', (index_code, str(cutoff)))]
    points = []
    for row in rows:
        counts = row['statistics'][basis]
        point = dict(date=row['trade_date'], index_close=float(row['index_close']), source=SOURCE,
                     member_count=row['member_count'], positive_count=counts['eligible'], loss_count=counts['loss'],
                     missing_pe_count=counts['missing_pe'], excluded_high_pe_count=counts['over_limit'])
        point.update({key: None if row[key] is None else float(row[key]) for key in METRICS})
        point.update({key+'_pe': point[key+'_pe_'+basis] for key in ('overall', 'mean', 'median')})
        points.append(point)
    values = [p['mean_pe'] for p in points if p['mean_pe'] is not None]
    first, last = (points[0]['date'], points[-1]['date']) if points else (None, None)
    return dict(points=points, latest=points[-1] if points else None, method=METHOD, index_code=index_code,
        index_name=INDICES[index_code][0], basis=basis,
        benchmarks=dict(p20=quantile(values, .2), p50=quantile(values, .5), p80=quantile(values, .8),
                        sample_days=len(values), start_date=first, end_date=last),
        coverage=dict(requested_start=INDICES[index_code][2], first_date=first, last_date=last,
                      missing_count=None, known_missing_count=0, calendar_complete=False),
        gap_dates=[], sources=[SOURCE] if rows else [])
