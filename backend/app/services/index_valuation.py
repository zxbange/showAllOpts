"""V2 calculated index summaries: bounded source reads, no member-history download."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import logging
import re
import struct
import time

from sqlalchemy import LargeBinary, MetaData, Table, cast, func, inspect, select
from sqlalchemy.dialects.mysql import CHAR
from sqlalchemy.exc import DBAPIError

from app.core.cache_errors import SnapshotDataError
from app.core.mysql_payload import decompress_json
from app.core.sync_progress import report

LOG = logging.getLogger(__name__)
PAGE_SIZE = 500

METHOD = 'index_pe_lyr_ttm_cap300_v2'
SOURCE = 'JQ_PLATFORM.VALUATION.V2'
PUBLIC_SOURCE = 'EASTMONEY.VALUATION.V2'
TABLE = 'valuation_ruler_daily_v2'
INDICES = {'000300.XSHG': ('沪深300', 300, '2010-01-01'),
           '000016.XSHG': ('上证50', 50, '2010-01-01'),
           '000852.XSHG': ('中证1000', 1000, '2014-10-17')}
METRICS = tuple(metric+'_pe_'+basis for basis in ('ttm', 'lyr') for metric in ('overall', 'mean', 'median') if metric != 'overall' or basis == 'lyr')


def init_store(db):
    db.execute('CREATE TABLE IF NOT EXISTS index_points_v2 '
               '(index_code TEXT NOT NULL,trade_date TEXT NOT NULL,payload TEXT NOT NULL, '
               'PRIMARY KEY(index_code,trade_date))')
    db.execute('CREATE TABLE IF NOT EXISTS index_sync_baseline_v2 '
               '(index_code TEXT PRIMARY KEY, method_version TEXT NOT NULL, start_date TEXT NOT NULL)')


def available(engine):
    return inspect(engine).has_table(TABLE)


def checked(row, cutoff):
    """Validate the collector contract; raw-input verification remains a source audit."""
    def reject():
        raise SnapshotDataError('指数估值v2汇总不完整或计算口径不匹配；已有缓存保留。')
    code, day = row['index_code'], row['trade_date']
    if (code not in INDICES or type(day) is not date or not date.fromisoformat(INDICES[code][2]) <= day <= cutoff
            or day.weekday() >= 5 or row['method_version'] != METHOD or row['source'] not in (SOURCE, PUBLIC_SOURCE)
            or row['member_count'] != INDICES[code][1] or not isinstance(row['updated_at'], datetime)
            or not re.fullmatch('[a-f0-9]{64}', row['members_sha256'])):
        reject()
    try:
        stats = json.loads(row['statistics'])
        if (stats['profit_basis'] != 'market_cap_divided_by_pe' or stats['market_cap_unit'] != '亿元'
                or stats['ordinary_trading_suspension_excluded'] is not False
                or stats['listing_status_basis'] not in ('historical_index_members', 'jy_listing_history', 'eastmoney_current_members')):
            reject()
        if (row['source'] == PUBLIC_SOURCE) != (stats['listing_status_basis'] == 'eastmoney_current_members'):
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
            fields = ('eligible', 'loss', 'over_limit', 'missing_pe', 'suspended_listing') + (('overall_valid', 'overall_missing') if basis == 'lyr' else ())
            if any(type(counts[k]) is not int or not 0 <= counts[k] <= row['member_count'] for k in fields):
                reject()
            if sum(counts[k] for k in fields[:5]) != row['member_count']:
                reject()
            if basis == 'lyr' and counts['overall_valid']+counts['overall_missing']+counts['suspended_listing'] != row['member_count']:
                reject()
            if any((result[m+'_pe_'+basis] is None) != (counts['eligible'] == 0) for m in ('mean', 'median')):
                reject()
            if basis == 'lyr' and counts['overall_missing'] and result['overall_pe_lyr'] is not None:
                reject()
        return result
    except (KeyError, TypeError, ValueError, InvalidOperation):
        reject()


def packed_query(query):
    """Compress the whole bounded page, including repeated JSON and field values."""
    page = query.subquery()
    values = func.json_array(*[cast(c, CHAR(charset='utf8mb4')) for c in page.c])
    return select(func.compress(func.json_arrayagg(values), type_=LargeBinary)).select_from(page)


def unpack_page(packet, names):
    if packet is None:
        return []
    try:
        # Bound expansion before decoding; one page normally expands to ~500 KiB.
        if len(packet) < 4 or struct.unpack('<I', packet[:4])[0] > 8 * 1024 * 1024:
            raise ValueError()
        values = json.loads(decompress_json(packet))
        if not isinstance(values, list) or len(values) > PAGE_SIZE:
            raise ValueError()
        rows = []
        for values_row in values:
            if not isinstance(values_row, list) or len(values_row) != len(names):
                raise ValueError()
            row = dict(zip(names, values_row))
            row['trade_date'] = date.fromisoformat(row['trade_date'])
            row['updated_at'] = datetime.fromisoformat(row['updated_at'])
            row['member_count'] = int(row['member_count'])
            rows.append(row)
        # MySQL does not promise JSON_ARRAYAGG order. Restore keyset order locally.
        rows.sort(key=lambda row: row['trade_date'])
        if len({row['trade_date'] for row in rows}) != len(rows):
            raise ValueError()
        return rows
    except (ValueError, TypeError, KeyError, struct.error, SnapshotDataError):
        raise SnapshotDataError('估值汇总压缩包不完整，已保留原缓存。') from None


def sync(engine, service, cutoff, check, progress, *, full=False, same_source=True):
    """Runs against copy_history's private SQLite snapshot; failure cannot publish it."""
    table = Table(TABLE, MetaData(), autoload_with=engine)
    compressed = engine.dialect.name == 'mysql'
    if compressed:
        try:
            with engine.connect() as connection:
                connection.scalar(select(func.json_arrayagg('probe')))
        except DBAPIError as exc:
            if getattr(exc.orig, 'args', (None,))[0] != 1305:
                raise
            compressed = False  # Older MySQL without JSON_ARRAYAGG: small typed pages.
    page_size = PAGE_SIZE if compressed else 100
    inserted = updated = processed = 0
    with service.db() as local:
        init_store(local)
        if not same_source:
            local.execute('DELETE FROM index_points_v2')
            local.execute('DELETE FROM index_sync_baseline_v2')
        for index, code in enumerate(INDICES):
            floor = date.fromisoformat(INDICES[code][2])
            baseline = local.execute('SELECT method_version,start_date FROM index_sync_baseline_v2 '
                                     'WHERE index_code=?', (code,)).fetchone()
            recent = local.execute('SELECT trade_date FROM index_points_v2 WHERE index_code=? '
                                   'ORDER BY trade_date DESC LIMIT 3', (code,)).fetchall()
            complete = baseline is not None and baseline[0] == METHOD and baseline[1] <= str(floor)
            start = date.fromisoformat(recent[-1][0]) if recent and complete and not full else floor
            # Public daily collection may have written just today's point. It is not
            # evidence that this MySQL history has ever been imported completely.
            source_range = (table.c.index_code == code, table.c.method_version == METHOD,
                            table.c.trade_date >= floor, table.c.trade_date <= cutoff)
            with engine.connect() as connection:
                first_source = connection.scalar(select(func.min(table.c.trade_date)).where(*source_range))
            first_local = local.execute('SELECT MIN(trade_date) FROM index_points_v2 WHERE index_code=?',
                                        (code,)).fetchone()[0]
            if first_source is not None and (first_local is None or str(first_source) < first_local):
                start = floor  # Historical backfill can arrive after the first daily sync.
            condition = (table.c.index_code == code, table.c.method_version == METHOD,
                         table.c.trade_date >= start, table.c.trade_date <= cutoff)
            check()
            progress('{}：核对增量范围'.format(INDICES[code][0]), 5 + index * 28)
            with engine.connect() as connection:
                total = connection.scalar(select(func.count()).select_from(table).where(*condition))
            done = 0
            after = None
            while True:
                check()
                message = '{}：日汇总 {}/{} 条'.format(INDICES[code][0], done, total)
                progress(message, 5 + index * 28 + 28 * done / max(1, total))
                query = select(*(c for c in table.c if c.name != 'overall_pe_ttm')).where(*condition).order_by(table.c.trade_date).limit(page_size)
                if after is not None:
                    query = query.where(table.c.trade_date > after)
                # Release the source connection before local processing/writes.
                started = time.monotonic()
                LOG.info('Valuation summary read index=%s after=%s start=%s limit=%s compressed=%s',
                         code, after, start, page_size, compressed)
                with engine.connect() as connection:
                    if compressed:
                        packet = connection.scalar(packed_query(query))
                        rows = unpack_page(packet, [c.name for c in table.c if c.name != 'overall_pe_ttm'])
                    else:
                        rows = connection.execute(query).mappings().all()
                LOG.info('Valuation summary received index=%s rows=%s seconds=%.3f',
                         code, len(rows), time.monotonic() - started)
                for row in rows:
                    check()
                    if row['index_code'] != code or row['trade_date'] < start or (after and row['trade_date'] <= after):
                        raise SnapshotDataError('估值汇总返回范围不匹配，已保留原缓存。')
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
                done += len(rows)
                message = '{}：日汇总 {}/{} 条'.format(INDICES[code][0], done, max(done, total))
                progress(message, 5 + index * 28 + 28 * min(1, done / max(1, total)))
                report(message, rows=len(rows))
                if len(rows) < page_size:
                    if done < total:
                        raise SnapshotDataError('估值汇总条数少于源库统计，本次未发布，请重试。')
                    break
                after = rows[-1]['trade_date']
            if start == floor:
                local.execute('INSERT INTO index_sync_baseline_v2 VALUES (?,?,?) '
                              'ON CONFLICT(index_code) DO UPDATE SET '
                              'method_version=excluded.method_version,start_date=excluded.start_date',
                              (code, METHOD, str(floor)))
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
        point = dict(date=row['trade_date'], index_close=float(row['index_close']), source=row['source'],
                     member_count=row['member_count'], positive_count=counts['eligible'], loss_count=counts['loss'],
                     missing_pe_count=counts['missing_pe'], excluded_high_pe_count=counts['over_limit'])
        point.update({key: None if row[key] is None else float(row[key]) for key in METRICS})
        point.update({key+'_pe': point[key+'_pe_'+basis] for key in ('mean', 'median')})
        if basis == 'lyr':
            point['overall_pe'] = point['overall_pe_lyr']
        points.append(point)
    values = [p['mean_pe'] for p in points if p['mean_pe'] is not None]
    first, last = (points[0]['date'], points[-1]['date']) if points else (None, None)
    return dict(points=points, latest=points[-1] if points else None, method=METHOD, index_code=index_code,
        index_name=INDICES[index_code][0], basis=basis,
        benchmarks=dict(p20=quantile(values, .2), p50=quantile(values, .5), p80=quantile(values, .8),
                        sample_days=len(values), start_date=first, end_date=last),
        coverage=dict(requested_start=INDICES[index_code][2], first_date=first, last_date=last,
                      missing_count=None, known_missing_count=0, calendar_complete=False),
        gap_dates=[], sources=sorted({row['source'] for row in rows}))
